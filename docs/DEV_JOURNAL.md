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

## 12.10: Pre-Launch Innovation Gate — 2026-08-06
**Status:** COMPLETE | **Gates:** mypy 0/461 · pyright 0 · pytest 2877 passed/2 skipped, zero footnoted flakes · npm compile 0 · npm lint 0 · npm test 191 passed
- Shipped: Phase 12 closure. Amended the gate's own "prompt caching tokens-saved metric > 0" criterion (CLAUDE.md §4 Option B) — 12.1's own measurement had already disproven the premise; DEBT-137 stays open with cause and a re-evaluation trigger, not silently closed. New `tests/test_phase12_checkpoint_gate.py` (17 tests) re-certifies 12.1–12.14's cross-cutting invariants. Translated 47 Spanish lines across 10 production files (§13.3), including the public FastAPI app description and `DirtyBuffer`/`IDEContext` OpenAPI field descriptions. Fixed six stale post-12.7 claims in `DEVELOPERS.md` and two stale manifest "Next action" pointers.
- Key decision: two genuine pre-existing defects surfaced by the full-suite run were fixed at the root rather than footnoted, per the zero-flake policy — `BackgroundTaskManager.stop()` only force-killed the Windows process tree when the shell's own `returncode` was still `None`, but `create_subprocess_shell` spawns cmd.exe as the direct child and the real command as a grandchild, so a cleanly-exited shell left an orphaned grandchild running for its full duration; `stop()` now tree-kills unconditionally on Windows. Separately, the new OS1 gate row assumed an empty `brain.agentic_cell._session_registry`, but several other test files write into that same process-wide global with no reset — made the row snapshot/clear/restore instead.
- Deferred: DEBT-161 (471 phase/ADR references across 130 production files, §13.1/§13.2 — measured, not swept, to avoid a pre-launch blast-radius spike) and DEBT-162 (three dead REST contract models in `api/api_contracts.py`, same shape as DEBT-144).

---

## 12.16: Testing & Debugging Rigor Hardening — 2026-08-04
**Status:** COMPLETE | **Gates:** mypy 0/460 · pyright 0 · pytest 2858 passed/2 skipped (91% line coverage) · npm compile 0 · npm lint 0
- Shipped: zero-flake policy in `DEVELOPERS.md` formalizing the DEBT-108/153 precedent (12.14) — re-verified both stay closed via their exact repro commands, in isolation and in the full suite. New `ailienant-core/pytest.ini` registers `unit`/`integration`/`e2e` markers for tests going forward (DEBT-157 logs that the existing ~2,858 tests are not retroactively classified — too large a task to do blind in this pass). Two Hypothesis property tests added to `tests/test_patcher.py` (round-trip apply/revert, always-reject-on-ambiguous-match) as the exemplar for future property coverage of the patch-safety surface. `docs/DEBUGGING_RUNBOOK.md` documents the exec-log ring, Glass-Box Timeline, audit chain, and telemetry tables as an install-triage map, plus a pointer to 12.14's new sandbox-reliability regression suites.
- Key decision: the first-ever coverage baseline (91%, `pytest --cov`) is recorded with an explicit caveat — the suite mocks the LLM/vector-store boundary uniformly (even files named `checkpoint_gate`/`e2e`), so a high line-coverage number does not mean the agent/LLM interaction surface is integration-tested end to end.
- Deferred: DEBT-157 (test taxonomy retrofit), DEBT-158 (Playwright e2e is one Dashboard-only spec).

---

## 12.17: Professional Dev-Environment Completion — 2026-08-04
**Status:** COMPLETE | **Gates:** mypy 0/460 · pyright 0 · pytest 2858 passed/2 skipped · npm compile 0 · npm lint 0
- Shipped: `.pre-commit-config.yaml` (ruff + mypy-on-changed-files + eslint) backed by a new `scripts/pre_commit_backend_gate.py` — a portable venv-resolving entry point, needed after the naive design (hardcoded venv exe paths, then a PowerShell wrapper for the mypy cwd-switch) hit a genuine Windows `CreateProcess` bug: a relative path with a subdirectory component fails to spawn, while a bare PATH-searched name works, so both hooks now route through one small Python script instead of shell-specific entries. Issue/PR templates, `SECURITY.md` (GitHub private vulnerability reporting), `.github/dependabot.yml` (pip/npm/github-actions), and `CODEOWNERS` all mirror `CONTRIBUTING.md`'s existing stated process rather than inventing new fields.
- Key decision: `CODEOWNERS` needed the maintainer's real GitHub handle, not the git-config email — asked rather than guessed.
- Deferred: DEBT-159 — pre-commit's mypy hook is a fast local approximation (changed files only); CI's full-tree `mypy .` remains authoritative.

## 12.15: CI/CD Pipeline Foundation — 2026-08-04
**Status:** COMPLETE | **Gates:** mypy 0/460 · pyright 0 · pytest 2858 passed/2 skipped · npm compile 0 · npm lint 0
- Shipped: `.github/workflows/backend-gate.yml` (ruff → mypy → pyright → pytest+cov, Python 3.13 to match the Dockerfile runtime, pip-cached) and `frontend-gate.yml` (npm ci → compile → lint → xvfb-run test; Playwright e2e nightly-only, provisioning its own backend venv since `run-backend.mjs` needs one on disk) — the repo's first enforced CI. `CONTRIBUTING.md`'s CLA claim corrected to describe only the working manual `CLA.md` path (no bot exists; DEBT-156 logged).
- Key decision: standing up CI surfaced four real, pre-existing gaps a `git diff`-only review would have missed — `requirements.txt` was UTF-16 (pip on Linux would have failed outright), `pywin32` had no `sys_platform` marker (would have failed installing on the Linux runner), and `mypy`/`pytest` were installed locally but never listed in `requirements.txt` at all. All fixed as part of this sub-phase since a CI that can't install is not CI. `ruff check .` also had 112 pre-existing errors across 48 files (never actually enforced despite being documented as a gate) — fixed individually rather than blind-`--fix`'d, after the auto-fixer's own first pass deleted a load-bearing re-export (`core.telemetry._MASK_INPUT_CAP`) that a test reached via module attribute access; caught by mypy, fixed at the root by importing the constant directly instead of suppressing the lint rule.
- Deferred: none; branch-protection required-status-checks is a manual GitHub Settings step, documented in `DEVELOPERS.md`, not committable.
- Also fixed (DEBT-160, HIGH): fixing the `pywin32` marker caused `pywinpty` to actually install for the first time in this dev environment, and pyright immediately flagged `core/pty_session.py::_WindowsPtyBackend.terminate_tree()`/`.wait()` calling `winpty.PtyProcess.kill()`/`.wait(timeout)` with signatures that don't exist on the real library (`TypeError` at runtime) — invisible because `mypy.ini` ignores the `winpty` import and the backend silently degrades to a non-TTY pipe on any construction failure. `_WindowsPtyBackend` had zero test coverage on any platform before this; two new real-ConPTY tests now cover it.
- First CI run — 2026-08-05: `shared/hardware.py`'s Windows-only `winreg` branch was guarded by `platform.system() == "Windows"`, which mypy doesn't statically recognize (unlike `sys.platform == "win32"`, this codebase's own established idiom elsewhere) — checked the branch unconditionally and failed on Linux's empty `winreg` stub; switched to the recognized guard. A new DEBT-160 regression test reached a `_WindowsPtyBackend`-only private attribute through the abstract `_PtyBackend` type — simplified the assertion instead. `esbuild.js` copied `dashboard/index.html` before its output directory was guaranteed to exist (masked locally by a stale `dist/` two weeks old); added the same `fs.mkdirSync(..., {recursive:true})` idiom `copyUserGuides()` already uses. All three verified against actual Linux-equivalent conditions locally (`mypy`/`pyright --platform linux`, full suite with `pywinpty` physically removed from the venv, `npm run compile` with `dist/` removed) before pushing again, not just re-tested on Windows.
- Second CI run — 2026-08-05: frontend gate went green; backend `pytest` (which never got this far before) surfaced 4 real failures — `tests/error_correction.py`'s traceback-based workspace filter (`os.path.relpath`) can't parse a foreign OS's path format, and two fixtures hardcoded Windows-style literals (`C:\ws\...`) that aren't real paths on the Linux runner; fixed by deriving host-appropriate paths at test time (`os.path.join(os.path.abspath(os.sep), ...)`) rather than literals. `tools/researcher_tools.py::_canon`'s case/separator-unification guarantee is NTFS-specific by design (its own docstring says so) — POSIX has no equivalent ambiguity to resolve, so its two directed tests are now `skipif`'d non-Windows rather than rewritten, matching the existing Unix/Windows-split convention in `test_phase7_19_0_pty_session.py`. Neither is a production bug; both are test-fixture platform-portability gaps that only a real Linux run — not local Windows testing or mypy/pyright platform simulation — could surface.

## 12.8: Fresh Debt Triage Sweep — 2026-08-04
**Status:** COMPLETE | **Gates:** mypy 0/459 · pyright 0 · pytest 2839 passed/2 skipped · npm compile 0 · npm test 191 passed
- Shipped: closed DEBT-121/123/124/128/132/134 outright, plus the display-wiring half of DEBT-125 and the tool-call half of DEBT-133 (file-content preview re-logged as DEBT-155, classifier as DEBT-154); DEBT-126's two halves resolved (real whole-turn duration) and corrected-as-stale (the dead handler it named no longer existed). `ToolDispatcher.dispatch` now instruments the Glass-Box Timeline directly — real detail bodies for every registry/MCP tool call across all three consumers 12.7 wired — and the devcontainer tier live-streams execution chunks via a new correlated `ContextVar` + WS event, bounded on both backend and frontend against a runaway command.
- Key decision: DEBT-122 (Rich Tool Chips) closed as superseded (§4 Pivot), not migrated — 12.7 gave `ToolDispatcher` three live consumers, so instrumenting `dispatch()` itself delivers the original goal without ever rerouting the main tool-call loop through `execute_tracked_tool`; dead `upsertToolBody` deleted with its test. A HITL-deferred tool call now mints its Glass-Box ref at defer time (a non-replayed super-step) and carries it through `pending_tool_call` so a LangGraph replay of the resume phase never opens a duplicate row.
- Deferred: DEBT-154 (semantic edit-risk classifier, regex proxy unchanged) and DEBT-155 (masked file-content preview, needs its own redaction design).

## 12.6: Sandbox Reliability Hardening — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/456 · pyright 0 · pytest 2778 passed/2 skipped · npm compile 0 · npm lint 0
- Shipped: closed DEBT-097 and DEBT-100, the last two HIGH-tier backlog items. `DockerSandboxAdapter` leases containers from a bounded `_ContainerPool` keyed by `(mount root, session)` instead of one shared process-lifetime container — fixes the cross-session noisy-neighbor/blast-radius risk and a latent wrong-mount bug (a second project's session silently fell back to the first project's `/workspace`). Every Docker SDK call now routes through a timeout-bounded, breaker-guarded dispatcher on a dedicated `ail-docker` thread pool, so a hung daemon degrades to `[sandbox_daemon_unavailable]` instead of parking a thread from the shared executor every other subsystem depends on.
- Key decision: pool exhaustion shares an existing lease only when it is mounted at the **same** root — sharing across mount roots was rejected even under contention, since it would execute a command against the wrong project rather than merely lose CPU/RAM isolation; that case fails closed to `[sandbox_pool_exhausted]` instead.
- Deferred: DEBT-150 (a hijacked interactive-PTY exec socket still leaks one thread on a daemon hang — the one Docker call a socket timeout cannot bound), DEBT-151 (same-mount sharing under exhaustion has no true admission queue), DEBT-152 (an unwired pre-existing `sweep_orphaned_sessions` TODO now lets an aborted run's PTY lease permanently occupy a bounded pool slot).

## 12.5: Quality & Polish Debt Sweep — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/456 · pyright 0 · pytest 2732 passed/2 skipped · npm compile 0 · npm test 186 passed
- Shipped: closed DEBT-045/047/067/012/079 and INVALID'd DEBT-052 — three of the six backlog entries described the system inaccurately (12.3's DEBT-049 pattern) and were corrected rather than implemented as literally written. `action_token_usage` telemetry + explicit gateway `action` tag calibrate `BudgetEstimatorTool`'s confidence from real history; a bounded, injected LRU (`_DescriptionEmbedCache`) plus an off-loop `core.tool_rag` import fix the skill resolver's real N+1/blocking-import defects (the "sync LanceDB" premise was false — `core/db.py` is aiosqlite); `DocstringGeneratorTool` now renders signature-aware Google/Numpy Args/Returns/Raises/Attributes sections and closes the single-line-def gap it used to SKIP; a new opt-in `scripts/hardware_stress_sim.py` applies real RAM/VRAM pressure (VRAM stress explicitly skips, not fakes, when no compute framework is installed); `DiffBlock.tsx` renders word-diff highlighting AND syntax color simultaneously via `dangerouslySetInnerHTML` (the library reconstructs the full line and overlays its own `<ins>`/`<del>` markup — it never calls `renderContent` per fragment, contrary to the prior code comment), with `react-diff-viewer-continued` pinned to its exact installed version and a characterization test guarding the coupling; DEBT-079 turned out to be a real correctness bug (an empty user message written into the persisted transcript on a restart-resume), fixed via two additive `AIlienantGraphState` channels. DEBT-014 measured (not assumed) still blocked — 78 errors/24 files today, worse than the 63/19 on record — re-logged with the corrected ignore-site count (6, not 3).
- Key decision: an in-flight draft threaded the coder's `action` tag through the gateway as an unconditional keyword — broke every hand-rolled test double mocking those methods with a fixed signature (2 `test_planner.py` regressions). Fixed by only ever forwarding it via `**extra` from a `total=False` TypedDict, so an untagged call is byte-identical to before at the call site.
- Deferred: DEBT-014 (still blocked on LangGraph's `NodeInputT` stub bound); new entry logged for graph-state/relational-config separation, triggered only once a third piece of per-task config needs cross-restart durability.

## 11.9: Dashboard Checkpoint Gate — 2026-07-31
**Status:** COMPLETE | **Gates:** mypy 0/449 · pyright 0 · npm compile 0 · npm lint 0 · npm test 148 passed · Playwright 4 passed
- Shipped: split the gate into two harnesses matching where each invariant actually lives — a new Playwright/Chromium suite (`ailienant-extension/e2e/`) against a hermetically-seeded backend (`ailienant-core/tests/e2e/seed_dashboard_fixture.py`) for the browser-reachable dashboard SPA (panels, project selector, GraphRAG graph + god-node badge, vector map), and a Mocha/jsdom suite (`phase11_9_dashboard_checkpoint_gate.test.ts`) for the VS Code webview components unreachable from a browser (`ActiveTaskHeader`, `ReasoningStream`, `SessionSummaryCard`, auto-accept), reusing the existing hand-rolled `createRoot` render pattern instead of adding a testing-library dependency.
- Key decision: corrected two stale DoD literals in-flight — `ReasoningStream` deliberately never surfaces native/simulated provenance (0948f35), so the gate asserts render-identity + `thinkingSource` state instead of a `[Simulated]` tag; the compaction threshold is 40 (`MESSAGE_COMPACTION_THRESHOLD`), not 60. Building the GraphRAG fixture surfaced a real pre-existing bug: `CodeGraphLayer.tsx`'s `onNodeClick` dropped `is_god_node` when reconstructing the clicked node for the detail panel, so the god-node badge silently never rendered via the 2D graph view (only the 3D nebula path, which forwards the node object unmodified, got it right) — fixed in the same diff.
- Deferred: DEBT-135 — the Playwright fixture seeds the DB/LanceDB stores directly via existing helpers rather than running the real indexer end-to-end. DEBT-136 — Chromium-only smoke, no cross-browser matrix.

## 11.5.D: Execution Provenance — Glass-Box Timeline I/O Detail — 2026-07-30
**Status:** COMPLETE | **Gates:** mypy 0/448 · pyright 0 · pytest 2653 passed/2 skipped (1 pre-existing unrelated flake, see note) · npm compile 0 · npm lint 0 · npm test 139 passed
- Shipped: the 11.5.C timeline's deferred `detail?` field — a `command` node now expands to show execution tier, cwd, initiator, stdout/stderr, exit code, and duration. New `server_activity_detail` WS event (SCHEMA_EVOLUTION §40), sole emitter `core/exec_log.py::record_execution`, correlated via a turn-scoped `ActivitySink` (`core/activity_context.py`, a `ContextVar`) so no LLM-facing tool signature changed. `record_exec` now returns the masked record it already built for the dashboard ring, so the WS and dashboard paths share one masking site.
- Key decision: caught in design review — dropping `_classify_activity`'s `"running "` verb naively would have made a permission-gate-denied or dangerous-pattern-intercepted command vanish from the timeline (both abort before `record_execution` runs). Replaced with a `"blocked "` verb instead, so a denied attempt surfaces as a distinct node rather than disappearing.
- Deferred: DEBT-132 — background-task executions (`BackgroundTaskManager` bypasses `record_execution`) get no detail box. DEBT-133 — file-read and MCP-tool-call I/O detail (different chokepoints, separate PII/budget call). DEBT-134 — live incremental output streaming (only the persistent-PTY path supports it today).
- Note: `tests/benchmark/test_retention.py::test_run_benchmark_bounds_artifacts` failed once under full-suite timing (Windows FileLock contention) and passed clean on two immediate reruns of the same file — confirmed non-deterministic and unrelated (no import path connects it to any file this division touched); consistent with the same flake already logged under Division 8.18.

---

## Division 8.18: CoderAgent Tool Activation — 2026-07-30
**Status:** COMPLETE | **Gates:** mypy 0/448 · pyright 0 · pytest green (full suite, see note)
- Shipped: closed the gap that "CoderAgent Role-Prompt Debiasing" (2026-07-28) surfaced — ~53 tool classes exist in `tools/*.py`, ~35 with zero production callers, almost entirely the Coder's. Root cause: three of the four links needed to connect the catalog to a dispatch loop were already built (`ToolRAGStore.select_tools`, `deferred_tool_loader`'s eager/deferred policy, `tool_rag_select_node`) but the fourth — a schema-name-to-constructed-tool bridge — never existed; `tool_registry_active` was a write-only state channel read by nobody. New `core/tool_registry.py::resolve_tools()` is that bridge, reusing each family's own `build_*_tools(state)` factory where one exists and wrapping the rest via the *selected* schema's own tier/roles (never a duplicated role constant); a small, reasoned `_INTENTIONALLY_UNREGISTERED` set (11 names) documents deliberate exclusions — tools redundant with `agentic_cell.py`'s own `apply_granular_edit` primitive, or owned by the separate `gateway/` package. `main.py`'s lifespan now populates the catalog at boot (`populate_tool_catalog`, made resilient to a missing embedding provider after it broke 13 tests that boot the real app under `TestClient`). The Coder's iterative path (`brain/agentic_cell.py`) gained an additive fallback branch — any tool name outside its 3 hardcoded primitives resolves through the registry and executes via the already-proven `ToolDispatcher`, with the 3 primitives' own dispatch branches left untouched. The Analyst gained 5 previously-orphaned perception tools via the same registry. A new reachability checkpoint gate (`test_phase8_18_checkpoint_gate.py`) asserts every `BaseTool` class discovered by walking `tools/*.py` is either resolvable or explicitly excluded — the first assertion in this codebase's history checking production reachability rather than mere existence.
- Key decision: dropped an earlier plan to reroute the Analyst/Researcher/subagent dispatch sites through retrieval — at this catalog size their visible tool sets sit under the eager-injection threshold, so retrieval would return the same set they already hardcode; churning working code for identical behavior wasn't worth the risk. Also dropped a hardcoded 13-ecosystem test-command detection table from the original draft of this division after external research (tool-count degradation studies, Anthropic's own tool-design guidance, harness-design literature) confirmed framework detection belongs to the model via a general `run_terminal` primitive, not a lookup table — and confirmed a large catalog behind a per-turn selector (already this codebase's design) is the correct, literature-endorsed shape.
- Deferred: DEBT-129 — interactive HITL-card approval for tools the Coder's fallback branch retrieves under a HITL-triggering session mode; safely denies today rather than risk a replay-unsafe mid-loop `interrupt()`. DEBT-130 — the Coder's one-shot `run_coder_node` path still has no tool-calling (only the iterative `agentic_cell.py` path was activated). DEBT-131 — decision record for the 11 tools in `_INTENTIONALLY_UNREGISTERED`. `tests/benchmark/test_retention.py::test_run_benchmark_bounds_artifacts` is pre-existing, unrelated flaky (file-lock contention race under full-suite timing; passes in isolation) — not touched.

---

## 11.13: Command Menu Completion & Polish — 2026-07-30
**Status:** COMPLETE | **Gates:** mypy 0/442 · pyright 0 · pytest 2624 passed/2 skipped · npm compile 0 · npm lint 0 · npm test 132 passed
- Shipped: full audit of the command menu closed every gap it found — a systemic resting-state color bug (`.ws-core-menu-btn`/`.ws-menu-back` were near-white until hovered; icons now carry the accent at rest, matching the root menu's own convention), outside-click dismissal + `aria-activedescendant` + an empty-results state, `Help documents` (previously always dead-ending) now opens bundled local guides via a new vscode-free `docsCatalog.ts`, the `/dev` shell-exec section is gated behind `ailienant.developerMode` (default off), three false "not yet wired" UI notes were corrected (hooks and MCP auto-connect were already real), and the two genuine MVP stubs — output style and per-role prompt overrides — now reach the LLM.
- Key decision: output style is injected only into the chat prompt, never the CoderAgent's — its SEARCH/REPLACE contract is machine-parsed and a style directive would fight it, the same failure class as an unscaffolded reasoning injection. A test locks the boundary. Role overrides resolve once per task into a loose `agent_role_overrides` state key (the `active_skills` pattern, since the catalog read is async and the prompt builder is sync/pure) and replace only the role directive, never `_BASE_CODER_PROMPT`.
- Deferred: DEBT-127 (dispatched subagents don't see role overrides, blocked on DEBT-106) and DEBT-128 (`analyst_name` persisted but inert — dashboard scope).

## CoderAgent Role-Prompt Debiasing — 2026-07-28
**Status:** COMPLETE | **Gates:** mypy 0/441 · pyright 0 · pytest 2602 passed/2 skipped
- Shipped: `qa_tester`'s directive named `pytest` specifically and `secops` named `Bandit`/`Semgrep` — neither tool exists in this codebase. Both reworded as content-generation guidance (infer the project's real test framework from the target file's language and neighboring tests; review each patch for language-appropriate OWASP risks) rather than instructions to invoke a specific tool by name. Also fixed `_BASE_CODER_PROMPT`'s stale "Emit unified diffs" line to match the actual SEARCH/REPLACE output contract.
- Key decision: an initial design generalized `tools/coder_tools.py::RunTestsTool`/`LinterAutoFixTool` (hardcoded to `pytest`/`ruff`) across 13 ecosystems, but investigation found that whole module has zero production callers — the CoderAgent has no tool-calling wiring at all (single-shot SEARCH/REPLACE text, not `bind_tools`). Building a generalized tool arsenal for tools nothing can call would have been effort spent on dead code; the design was preserved as the binding spec for Division 8.18 instead of discarded or built prematurely.
- Deferred: Division 8.18 — CoderAgent EXECUTE-Tier Tool Arsenal Correctness, gated on real tool-calling landing for the coder.

## 11.12: Complete Stack Guidance (11.11 Item D follow-up) — 2026-07-28
**Status:** COMPLETE | **Gates:** mypy 0/441 · pyright 0 · pytest 2602 passed/2 skipped · npm compile 0 · npm test 126 passed
- Shipped: a user audit found 11.11's stack guidance covering only 3 named artifact classes with no propagation path. A hardcoded stack catalog was proposed and rejected (token cost on every planner call, staleness, overrides the model's own knowledge). Two real gaps fixed instead: `MissionSpecification.decisions` was write-only across the whole backend (planner fills it, frontend renders it, no agent read it back), and `engine.py` routes `requires_iteration` steps to `brain/agentic_cell.py`, a second code generator that never saw mission context at all. `_STACK_GUIDANCE_DIRECTIVE` rewritten as an open-ended procedure (classify → constrain → choose → record as `decisions[0]` → require `target_file` consistency); new `MissionSpecification.to_context_block()` (`brain/state.py`) propagates the bounded decision/constraint block to both `agents/coder.py`'s budget-guarded prompt and `brain/agentic_cell.py`'s per-iteration ReAct messages.
- Key decision: rejected the hardcoded catalog design I had proposed in conversation before reading the code — the real defect was procedural (nothing asked the model to classify before choosing) and structural (the choice never reached either code generator), not a knowledge gap a catalog would have filled.
- Deferred: none — no new tactical patches or technical debt introduced this phase.

## 11.11: Agent Output Quality & Narration Depth (Phase 2) — 2026-07-28
**Status:** COMPLETE | **Gates:** mypy 0/441 · pyright 0 · pytest 2588 passed/2 skipped · npm compile 0 · npm test 126 passed
- Shipped: the five behavioral defects from the same live-test sweep. Analyst free-form narration streamed to the Thought Box via a second `astream_reasoning(free_form_answer=True)` pass, gated by the turn's own Reasoning Mode toggle (the Coder's own SEARCH/REPLACE generation never narrates on non-native models — strict contracts stay un-scaffolded by design). Cross-project RAG bleed fixed via `core/utils.py::filter_relevant_snippets` (top-level path scoping, explicit mentions always win) wired into both the coder and chat-question RAG paths. Coder/planner output ceilings now scale with step/request complexity instead of a flat 4096, hard-capped at half the resolved model's real context window. Chat answer depth is prompt-adaptive on `_EXPLAIN_SIGNALS`. Planner gained stack-choice guidance (infer from artifact class) and proportional (not uniformly-minimal) WBS scope discipline.
- Key decision: exploration overturned 3 of the original brief's root causes before any code changed — `fast_path.py` is SEQUENTIAL-mode only (never the question path), `_PLANNER_REASONING_MAX_TOKENS` budgets the pre-draft narrative not the plan, and the workspace tree/RAG query were already bounded (the real bleed vector was `project_id` granularity). Corrected in the blueprint and manifest spec before implementation.
- Deferred: none — no new tactical patches or technical debt introduced this phase.

## 11.10: Live-Test Correctness Sweep (Phase 1) — 2026-07-28
**Status:** COMPLETE | **Gates:** mypy 0/441 · pyright 0 · pytest 2551 passed · npm compile 0 · npm test 126 passed
- Shipped: nine defects from two live test runs fixed: AUTO summary/actuation divergence (verdict-driven unification), companion timeout tier-aware (45s local vs 12s cloud), token bucketing (resolved BYOM is_local, not alias name), absolute token ceiling on telemetry, OCC/context ring stale (post-write refresh + health verdicts), indexing pill silent (broadcast to active session), intent misroute (explanation-signal detection), stale-guard BOM false positive (UTF-8 BOM stripping on both sides), and timeline 0.0s (shared batch timestamp). Added 10 new regression tests covering the bounds and fixed 3 existing tests that were encoding the bugs. No new technical debt.
- Key decision: token bucketing fix revealed that tests read ambient BYOM config (local Ollama tiers) rather than isolating fixtures — corrected via `patch(get_chat_target, return_value=None)` + added new BYOM-resolution regression tests; three failing tests became fixtures, never regressions.
- Deferred: DEBT-126 — minor investigation backlog (turn-duration measure span, dead `server_indexing_started` handler).

## 11.8: Auto-Accept Shift-Left — 2026-07-27
**Status:** COMPLETE | **Gates:** mypy 0/440 · pyright 0 · pytest 2541 passed/2 skipped · tsc 0 · eslint 0 · npm compile 0
- Shipped: `autoAcceptLowRisk` now rides `TaskPayload` client→host→backend; the apply edge judges each pending edit's **added diff lines** against `permissions.py::scan_risk_patterns` (new shared helper) and, when set and no pattern matches, applies server-side with zero `server_hitl_approval_request` emissions — the blast-radius gate still guards both paths. Removed the prior webview short-circuit that read a never-sent `risk_metrics` field (vacuously true, so it silently auto-accepted every edit regardless of risk).
- Key decision: judged risk on added `+` lines only, not whole-file content — `_RISK_PATTERNS` is command-tuned, so scanning full files would false-positive on any file merely mentioning `api_key`/`.env` and make the feature nearly inert. Diffs are computed once and shared between the risk scan and the (still-possible) approval card.
- Deferred: DEBT-125 — the gate is a regex proxy over `_RISK_PATTERNS`, not a real edit-risk classifier; also fixes the `risk_metrics`↔`risk_patterns_matched` name mismatch so the card can show why an edit was flagged.

## 11.7: Chat Compaction for Long Sessions — 2026-07-27
**Status:** COMPLETE | **Gates:** mypy 0/440 · pyright 0 · pytest 2539 passed/2 skipped · tsc 0 · eslint 0 · npm compile 0
- Shipped: pre-compaction messages now fold behind a new collapsible `SessionSummaryCard.tsx` once the transcript exceeds `MESSAGE_COMPACTION_THRESHOLD` (40) and a `state_compacted` event has fired — non-destructive (bubbles stay in the store, restorable via "Show original messages"); short sessions render the marker as today's inline chip, unchanged. The AI prose the summarizer already produces is now plumbed to the wire via an additive optional `summary_text` field (`StateCompactedPayload` → `broadcast_state_compacted` → `summarizer.py::_emit_compacted`), since `state_compacted` previously carried only a status line, not the prose.
- Key decision: the manifest's claim that the event "carries the summary text" was false — the prose lived only in backend LangGraph state. Fixed at the source rather than working around it, since deferring it further would have left `SessionSummaryCard` showing a near-redundant status line. Two stale file refs also corrected: state/render live in the memory-only `chatStore.ts`/`types.ts` + `Workspace.tsx`, not `NattCanvas.tsx`/`workspaceStore.ts`.
- Deferred: DEBT-124 — the fold is live-session only (its marker is a transient `SystemMessage` excluded from `PERSIST_TRANSCRIPT`); a reload unwinds it back to the full transcript, no data lost.

## 11.6: Active Task Header / Prompt Preservation — 2026-07-27
**Status:** COMPLETE | **Gates:** tsc 0 · eslint 0 · npm compile 0 (frontend-only; no backend/pytest surface)
- Shipped: new `components/ActiveTaskHeader.tsx` — a sticky card pinned above `.ws-messages` that preserves the submitted prompt so it never scrolls out of view (DEBT-058). Expanded while the turn streams (reused `ReasoningGlyph` + "Working…" + live `m:ss` elapsed + Cancel wired to the existing `handleAbort`), collapsed to a one-line summary on `server_stream_end`, and pinned until the user dismisses it (`✕`) or a new submit supersedes it. State is additive `activeTaskPrompt`/`activeTaskStartedAt` in the memory-only `chatStore.ts`, set in `submitWithMode`; elapsed reuses the ReasoningStream interval idiom (ticks while streaming, frozen on settle). New `.ws-active-task` CSS (accent left rule, two-line prompt clamp, reduced-motion honored).
- Key decision: state lives in `chatStore.ts` (live/transient), NOT the manifest-named persisted `workspaceStore.ts` — a persisted prompt would resurrect a stale header after a panel reload. Two other stale manifest refs corrected in-place: mount is `Workspace.tsx` (main list), not `NattCanvas.tsx` (analyst side-pane); "TASK_COMPLETE" resolves to the real `server_stream_end` signal. The plan-accept `AGREEMENT_SIGNAL` submit shows a readable "Applying approved plan" label instead of the raw phrase.

## 11.5.C.4: Agent Activity Timeline — CellAuditWidget migration, 11.5.C closed — 2026-07-27
**Status:** COMPLETE | **Gates:** mypy 0/440 · pyright 0 · pytest 2539 passed/2 skipped (+2 new) · tsc 0 · eslint 0 · npm test 125 passed (+5 new)
- Shipped: `LiveCellDispatcher.emit_tool_call_start` now fires one `"cell"` activity marker per agentic-cell iteration (`ref=cell:{iteration}`, `target=tool_name`, `metric="iteration N"`), threaded from the turn-local `_push_activity` closure via an optional, guarded constructor param. Frontend: new `upsertCellBody` (mirrors `upsertToolBody`) correlates it order-agnostically into `TimelineEntry.cell`; `AgentTimeline` renders a `cell` row reusing `CellAuditWidget` fed a synthetic single-iteration run — no changes to `CellAuditWidget.tsx` itself. `Workspace.tsx`'s standalone `CellAuditWidget` sibling is removed; `m.cellRun`/`attachOrUpdateCellRun` kept as the internal computation vehicle the dual-write reads back from. 11.5.C flipped to `[x]`.
- Key decision: **descoped to cell-only, found during planning.** The original spec bundled Rich Tool Chips with the cell audit, but `execute_tracked_tool` (Tool Chips' only live producer) is a standalone dev-palette smoke command, deliberately excluded from the turn/abort-mesh — there is no turn-scoped `_push_activity` closure for its marker to belong to. `DEBT-122` narrowed to cell-only (resolved); Tool Chip migration is architecturally blocked pending a future, separately-scoped reroute of the main tool-call loop through `execute_tracked_tool`, not merely deferred. Also fixed: neither `'reasoning'` nor `'cell'` entries ever transition `status` from `'active'` to `'done'` in the data model, and the active-dot CSS pulse is infinite — the cell row derives its dot status from the same liveness check already computed for `CellAuditWidget`'s `streaming` prop, rather than trusting the stored status (reasoning's identical pre-existing gap is untouched, out of scope).

## 11.5.C.3: Agent Activity Timeline — checkpoint gate + closure — 2026-07-27
**Status:** COMPLETE | **Gates:** mypy 0/440 · pyright 0 · pytest 2537 passed/2 skipped (+6 new checkpoint-gate rows) · tsc 0 · eslint 0 · npm test 120 passed (net: +1 persistence test, -4 deleted PipelineProgress tests)
- Shipped: `tests/test_phase11_5_c_checkpoint_gate.py` — 6 certified rows (UNTHROTTLE1, SEQ1, ENUM1, CAP1, ADDITIVE1, NARRATE1) pinning the architectural invariants a future refactor could accidentally remove. Retired the parallel `server_pipeline_step` emission from `_narrate` (its only consumer, `PipelineProgress`, was replaced by `AgentTimeline` in 11.5.C.2) — `test_token_batcher.py`'s T7 ordering test now asserts the un-throttled `server_activity_event` channel instead. **Fixed a real correctness gap found during closure:** `m.timeline` was never persisted (excluded from `PERSIST_TRANSCRIPT`'s allowlist), so a rehydrated session (reopen VS Code, revisit a tab) would have shown NOTHING for a past turn's plan/diff — the standalone `ExecutionChecklist`/`DiffBlock` renders that used to cover this were removed in 11.5.C.2's swap. Fixed by persisting `timeline` with `stripReasoningForPersist` (drops 'reasoning' entries only, matching `thinking`'s pre-existing display-only exclusion; every other kind is now the durable audit record `checklist`/`diffBlocks` used to be). Deleted the now-dead `PipelineProgress.tsx` + its test + orphaned `.ws-thinking*`/`.ws-diff-stack` CSS (Boy Scout Rule).
- Key decision: `ACTIVITY_CAP` extracted from a closure-local to a module-level constant in `task_service.py` — purely so the 500-event cap is patchable in a fast test (`patch.object(ts_mod, "ACTIVITY_CAP", 3)`) instead of requiring a slow 501-iteration integration test. No behavior change. `NarrationGate`/`gate.record_answer(...)` bookkeeping is left in place as vestigial dead code (DEBT-123) rather than threading its removal through ~8 call sites in the same pass as the retirement — a deliberately scoped, tracked deferral.
- Deferred: **11.5.C itself is NOT flipped to `[x]`** — its own spec still names all six widgets, and Rich Tool Chips/`CellAuditWidget` remain unmigrated (DEBT-122, new manifest sub-item `11.5.C.4`: needs its own backend `ref`-correlated marker emission, a distinct mechanism from the coder's `run_command` narration path already wired). `upsertToolBody` is implemented and unit-tested, ready for that marker once it exists.

## 11.5.C.2: Agent Activity Timeline — AgentTimeline component + partial widget swap — 2026-07-27
**Status:** COMPLETE (partial swap, declared) | **Gates:** tsc 0 · eslint 0 · npm test 123 passed (+8 new, zero regressions)
- Shipped: new `AgentTimeline.tsx` — the living-spine renderer for `m.timeline` entries: a status-dot spine (pending→active-pulse→done/failed, one-shot arrival "ping"), a header with the infinity glyph (`ReasoningGlyph`) self-tracing while any row is active and settling to rest on completion, honest collapse-to-summary on done ("Worked for Ns · N actions · N files changed" — real counts, distinct files, never a throttled narration count), follow-scroll (auto-tracks new rows, detaches the moment the user scrolls up), and `prefers-reduced-motion` support throughout (`workspace.css`). `Workspace.tsx` now renders `<AgentTimeline>` in place of the standalone `PipelineProgress`/`ReasoningStream`/`ExecutionChecklist`/`DiffBlock` stack — reasoning and plan rows reuse `ReasoningStream`/`ExecutionChecklist` directly (their own tested settle/elapsed logic, untouched); diff rows reuse `DiffBlock` with IDENTICAL HITL wiring (`hitlActive`/`onRespond`/`onRequestChanges`) relocated, not rewritten. The pending-approval HITL-preview diff path (`server_hitl_approval_request`) is now ALSO correlated into `timeline` (`upsertDiffBody`, same `file_path` ref the post-apply marker later resolves onto), so the approval card and its diff render from the same entry whether proposed or applied.
- Key decision: **partial swap, explicitly scoped.** `ToolChip`/`ActionLog` (Rich Tool Chips) and `CellAuditWidget` (agentic-cell audit) are NOT migrated into the timeline this slice — no backend activity marker emits `ref = tool_call_id` yet (Rich Tool Chips is a separate mechanism from the coder's `run_command` narration path that feeds `server_activity_event`), so there is nothing to correlate against; migrating their RENDER without real correlation would mean synthesizing entries with no true `seq`, defeating the chronological-interleave premise. Both stay as unchanged, working siblings below `AgentTimeline` pending their own marker emission.
- Deferred to 11.5.C.3: retiring the parallel (now-redundant) `server_pipeline_step` emit — touches `NarrationGate`'s own dedicated `test_token_batcher.py` suite, distinct work from the widget swap, not rushed into this pass; `PipelineProgress.tsx`/its test file are unreferenced but not deleted yet (same deferred-cleanup pattern as prior slices); the checkpoint gate itself; Tool/Cell timeline migration (needs its own marker emission first).

## 11.5.C.1: Agent Activity Timeline — plan/diff/reasoning markers + frontend model — 2026-07-27
**Status:** COMPLETE | **Gates:** mypy 0/439 · pyright 0 · pytest 2530 passed/2 skipped (+9 net new, 1 test-double signature fix; 1 pre-existing unrelated benchmark-retention flake, isolated re-run confirmed green) · tsc 0 · eslint 0 · npm test 115 passed (+10 new)
- Shipped: the deferred 11.5.C.0 markers now emit with real `ref` correlation — `plan` fires once as soon as the WBS exists (the existing early-seed latch), `metric=f"{N} steps"`; `diff` fires once per file that actually lands on disk (`ref=target=file_path`, `metric="+N -M"` via new `_diff_line_delta`); `reasoning` fires once per `_ThinkingStreamer` span via an injected `on_span_start(ref)` hook, with the SAME ref stamped on every delta of that span (`ThinkingChunkPayload.ref` / `NattThinkingChunkPayload.ref`, additive). On the frontend: `TimelineEntry` data model (`shared/config.ts`) + `timeline` field on `ConversationMessage`; `server_activity_event` wired into `contracts.ts` + the stall-watchdog allowlist; a pure, fully unit-tested `timelineBuilder.ts` (`upsertActivityMarker`/`upsertReasoningDelta`/`upsertDiffBody`/`upsertToolBody`) wired into `useWSMessageHandler` alongside the existing `thinking`/`diffBlocks` field updates (additive — no existing widget's behavior changes). Data-model + ingestion only — no renderer consumes `timeline` yet.
- Key decision: **order-agnostic correlation is real, not aspirational** — a body event (thinking delta, diff) can arrive before or after its marker over independent WS channels; every `upsertX` keys by the SAME wire id both sides use (thinking span `ref`, diff `file_path`) and creates an unresolved placeholder (`seq = +Infinity`, sorts last) on first sight of either, which the other side later adopts in place — proven by dedicated marker-first AND body-first tests for both reasoning and diff. `tool` correlation (`ref = tool_call_id`) is implemented and tested but not yet wired into a live handler case — no activity marker emits a tool ref this slice (Rich Tool Chips is a separate mechanism from the coder's `run_command` narration path), so there's nothing yet to correlate against; paired with a future tool/cell activity-marker emission.
- Deferred: `AgentTimeline` component + the six-widget swap + checkpoint gate (11.5.C.2–.3). Scoped simplification carried from backend: one reasoning `ref` per `_ThinkingStreamer` instance (per turn), not per distinct burst — two spans in one turn still group under one timeline node.

## 11.5.C.0: Agent Activity Timeline — backend activity channel — 2026-07-26
**Status:** COMPLETE | **Gates:** mypy 0/438 · pyright 0 · pytest 2528 passed/2 skipped (+5 new) · additive contract §38
- Shipped: the enabling backend slice of the Glass-Box Timeline — a new un-throttled `server_activity_event` WS event (`ActivityEventPayload{session_id,seq,ts,kind,target,metric,ref}` + `ActivityKind` Literal, `api/ws_contracts.py` §38) emitted via `broadcast_activity_event` (un-gated). A turn-local monotonic `seq` orders events; a 500/turn cap + sentinel keeps the un-gated channel bounded. The fix for the real bottleneck (the NarrationGate throttles `server_pipeline_step` to ≤15% of answer bytes, dropping most of the action trace) is that this channel bypasses the gate entirely.
- Key decision: **centralized classification, zero node churn.** Rather than re-point every `_emit`/`_narrate` site across planner/coder/error_correction/ideation (which would break every narration test that injects `narrate`), a single choke point — `core/task_service.py::_narrate` — classifies the raw node label via the pure module-level `_classify_activity(raw)→(kind,target,metric)` and feeds BOTH channels (un-throttled activity + legacy throttled pipeline step). Nodes are unchanged; PipelineProgress keeps working; the frontend composes the human label from `kind` so no raw token (`context_gather`) can ever reach the screen.
- Deferred: `reasoning`/`plan`/`diff` marker emission (with `ref` correlation) lands in 11.5.C.1 alongside the frontend consumer, where it's testable end-to-end (no observable effect until then). Full frontend timeline + widget swap + checkpoint gate follow in 11.5.C.1–.3.

## Fix: `npm test` broken suite-wide (bundle test files with esbuild) — 2026-07-26
**Status:** COMPLETE | **Gates:** check-types 0 · lint 0 · npm compile 0 · npm test 105 passed/0 failed (was: 0 ran, crashed on file #1)
- Shipped: `npm test` crashed on the first test file with `ERR_MODULE_NOT_FOUND` — `tsc -p . --outDir out` (via `"module": "Preserve"`) emitted extensionless relative imports that Node's ESM loader (used by `@vscode/test-cli`'s Electron host) refuses to resolve, unlike CommonJS `require()`. New `esbuild.tests.js` bundles each `src/test/*.test.ts` independently into a self-contained CJS file (mirroring the production `extension.js` bundle, which already proved esbuild resolves `shiki`'s ESM exports maps that plain `tsc` can't) — no runtime module resolution is left to trip over. `compile-tests` now runs it instead of raw `tsc`; `tsconfig.json`, `check-types`, `.vscode-test.mjs`, and the production `esbuild.js` are all untouched.
- Key decision: `jsdom` is marked `external` (alongside `vscode`) — jsdom loads its own internal assets (default stylesheet, sync XHR worker) via `__dirname`-relative paths at runtime; bundling it in rewrites `__dirname` to `out/test/` and breaks that resolution (`ENOENT: default-stylesheet.css`). Left external, Node's normal `require('jsdom')` keeps jsdom's real `__dirname` intact.
- Deferred: `watch-tests` script left on the old `tsc` path (dev-convenience only, not on the CI/pretest critical path); esbuild watch-mode for glob entry points needs its own verification pass.

## Fix: false stale-guard + live coding reasoning + reasoning badge + status relabel — 2026-07-26
**Status:** COMPLETE | **Gates:** mypy 0/438 · pyright 0 · pytest 2523 passed/2 skipped (+6 new) · tsc 0 · eslint 0
- Shipped (A, stale-guard): approving a diff falsely reported "these files changed since the proposal" on an unchanged file. Root cause: the write pipeline's stale guard compared two *different readers* — the backend captured `base_hash` via `make_safe_reader`→`read_safe`, whose disk read never joined `project_root`, so a relative `target_file` (`fibonacci.py`) resolved against the backend CWD, read as absent, and hashed `""`; the host hashes the real file after `joinPath(workspaceFolder, path)`. Fix: `read_safe` now resolves a relative path against `project_root` (the host's rule) before size/read/RAM/ignore — one change that also gives the coder the real file content (real refactor delta, not a full create) and fixes the diff old-side. Hits AUTO + ASK.
- Shipped (B, reasoning): non-native models showed no reasoning during coding tasks (the coder/planner emit strict output that can't be safely scaffolded — the 11.5.1 invariant). Planner now runs a separate, gated (non-native + wired sink), token-by-token live free-form reasoning pass before the WBS draft, on the un-throttled thinking channel. Native models skip it (they stream reasoning free); no sink → byte-identical to before.
- Shipped (C/D): removed the `Simulated`/`Native` provenance badge from `ReasoningStream`; retired the pipeline widget's raw `context_gather` jargon (friendly-label map) and its false "N steps" count (self-hides on done when a checklist exists). Boy-Scout: removed a stray path-string line in `editor/vfs_reader.ts` (last eslint warning) + translated a Spanish comment.
- Key decision: aligned the backend reader to the host (not a host-authoritative base_hash round-trip) — simpler and correct for AUTO where there is no HITL round-trip.
- Deferred: 11.5.C — Agent Activity Timeline (Glass-Box Transcript): a live, chronological, spined transcript unifying the six per-turn transparency widgets, gated by a new un-throttled `server_activity_event` channel (the NarrationGate throttle is why the current trace is sparse). Added to the manifest as a tracked sub-phase; Fix D-now is its interim down-payment. NOTE: the mocha/vscode-test harness is pre-existing-broken suite-wide (tsc `module: "Preserve"` emits extensionless imports the ESM test loader can't resolve); frontend verified via tsc + eslint; harness fix (move the test build to esbuild) is separate infra work.

## Fix: Multi-step WBS execution ran only step 1 (RELAY loop-back) — 2026-07-25
**Status:** COMPLETE | **Gates:** mypy 0/437 · pyright 0 · pytest 2516 passed/2 skipped (+7 new; 7 existing coder tests updated from the in-place-mutation assertion to the durable mission_spec-delta contract; 1 pre-existing unrelated benchmark-retention flake) · tsc/eslint unaffected
- Shipped: the production graph executed exactly one WBS step per turn then hit `END` — multi-step coding tasks never completed on the RELAY (local) path (masked by single-step plans). Root cause: `route_after_validation` only retried the same step or ended; nothing advanced to the next pending step, and `run_coder_node` marked step status by **in-place mutation** (never a returned `mission_spec` delta), which the `HybridCheckpointer` cannot durably capture. Fix (in-graph, no new node): coder now writes status as an immutable `mission_spec` delta via the reused `orchestrator._mark_step_status`; `route_after_validation` loops back to `drift_gate` while a `pending` step remains (re-dispatch + re-check finops/budget each iteration), with a stall guard, else `END`; same-file edits across steps compose (edit baseline = latest in-run `pending_contents`) while `base_hash` stays anchored to the true committed file; read-only steps now emit their checklist mutation.
- Key decision: kept the loop inside the graph via conditional edges (rejected an outer `astream` driver — it fights checkpoint semantics and would re-plan). Termination is guaranteed four ways: monotonic terminal-status progress, the stall guard, the per-iteration FinOps budget hard-kill (`supervisor_node`), and the guardrail's per-step retry cap. The previously-unwired `orchestrator._mark_step_status` is now reused in production.

## 11.5.1: Reasoning-scaffold safety hardening (DEBT-013 recurrence) — 2026-07-25
**Status:** COMPLETE | **Gates:** mypy 0/434 · pyright 0 · pytest 2487 passed/2 skipped (+9 net new; 1 pre-existing unrelated benchmark-retention flake, verified untouched) · tsc/eslint unaffected
- Shipped: live testing found 11.5's simulated-reasoning scaffold competing with strict output contracts on non-native models — Planner's `MissionSpecification` silently dropped required `target_file` fields (3/3 retries), and the Coder likely produced zero parseable edits (its own "no prose before or after" instruction directly contradicted the scaffold). Same failure class as DEBT-013 (thinking + strict JSON, previously fixed on the native path), recurring on the new simulated path. Fixed structurally, not per-caller: `astream_reasoning` now defaults to `free_form_answer=False` (no scaffold unless a caller explicitly proves its answer is free markdown); `response_format` unconditionally overrides even an explicit opt-in and now routes through `ainvoke` (restoring provider-level JSON enforcement + self-heal, the true pre-11.5 behavior for that case).
- Key decision: Planner, Coder, and `subagent_worker_node.py` needed zero code changes — they're protected automatically by the new safe default. Only the two genuinely free-form callers (`agents/analyst.py`, `core/task_service.py::_stream_with_thinking`) needed an explicit `free_form_answer=True` opt-in, which is the intentional, auditable declaration a safe API should require.
- Deferred: none — fully fixed, not a tradeoff.

## 11.5: Verbal Reasoning Fallback + Unified Reasoning Stream — 2026-07-25
**Status:** COMPLETE | **Gates:** mypy 0/434 · pyright 0 · pytest 2481 passed/2 skipped (+16 new) · tsc 0 · eslint 0
- Shipped: one shared `LLMGateway.astream_reasoning` engine that picks the model's native reasoning OR a prompt-scaffolded simulated `<thinking>` trace (split live by `tools/thinking_demux.py`), consumed by all three surfaces (planner/coder turns, direct live-chat, analyst pane); a unified borderless inline `ReasoningStream` + self-tracing infinity glyph (no emoji) replaces the boxed ThoughtBox and renders identically in the main chat and `NattCanvas`, differing only by an honest additive `[Native]`/`[Simulated]` `source` tag.
- Key decision: capability-gate + scaffold live only in the gateway so native and simulated are mutually exclusive (planner/coder prompts untouched, no double-injection); scaffold elicits conceptual, code-free reasoning; the existing Native-Thinking toggle stays the sole master switch — no new UI control (the manifest's Native/Verbose/Compact toggle was de-scoped by the user).
- Deferred: DEBT-057 closed; simulated max_tokens gets `+min(budget, 4096)` headroom so reasoning never starves the answer.

## 11.4: BYOM & Extensions Polish — 2026-07-24
**Status:** COMPLETE | **Gates:** mypy 0/431 · pyright 0 · pytest 2465 passed/2 skipped (+8 new) · tsc 0 · eslint 0
- Shipped: frontend visual+UX redesign of both dashboard panels onto the 11.0 primitives/tokens — BYOM reorganized into a Connect→Configure→Verify 3-step spine with a KPI row, a one-action quick-connect strip, a prominent active-preset summary, and per-model cost badges; Extensions got a KPI count row, tier/reachability `Badge`s, `EmptyState`s, and a skills search + collapsible create. Only backend touch: `core/config/model_pricing.py` (`price_for`) reading `litellm.model_cost`, surfaced as an additive `model_pricing` map on `BYOMConfigResponse`.
- Key decision: honest-substrate cost only — canonical ids are matched to litellm keys via a candidate ladder (normalized→verbatim→bare stem), local providers are free without touching litellm, and an unresolved model is omitted (no badge) rather than shown a guessed rate.
- Deferred: per-model benchmark Pass@1 (report is arm-keyed, not per-model), skill usage-stats, and the tool-catalog/semantic-search item all explicitly de-scoped by the user — not logged as debt.

## 11.3.B.3: Per-exec Command Log — 2026-07-23
**Status:** COMPLETE | **Gates:** mypy 0/429 · pyright 0 · pytest 2457 passed/2 skipped (+10 new) · tsc 0 · eslint 0
- Shipped: bounded in-memory `core/exec_log.py` ring (non-persistent, no retention debt) fed by a shared `record_execution(...)` wrapper at the 6 project-work `execute()` callers (source-tagged); cursor-paged `GET /api/v1/runtime/exec-log?since=&tail=N`; command+output masked/truncated before the lock; `RuntimePanel` "Sandbox command log" card; masker extracted to shared `core/redaction.py`.
- Key decision: capture at the callers (not a cage-adapter template-method — 13-site blast radius) and exclude the benchmark eval harness so the operator log isn't flooded; cursor keys off a monotonic `seq`, not the display timestamp, so an idle poll returns nothing.
- Deferred: DEBT-119 closed; the ring is in-memory/self-evicting so DEBT-120 (persisted-table retention) is unaffected.

## 11.3.B.1 + 11.3.B.2: Monitoring Backend Telemetry — 2026-07-22
**Status:** COMPLETE | **Gates:** mypy 0/426 · pyright 0 · pytest +12 new · tsc 0 · eslint 0
- Shipped: additive `request_latency` (project-scoped) + `container_lifecycle` (machine-global) telemetry tables in `core/telemetry.py` with hand-rolled `_percentile` (no numpy, §9); `_run_coding_task` `perf_counter`/`finally` latency emit; sandbox async-wrapper lifecycle emits; `GET /telemetry/latency` + `/runtime/lifecycle`; live TelemetryPanel latency card + RuntimePanel span timeline (open spans → now).
- Key decision: scope split by risk — set-tier (DEBT-118) dropped won't-do (would silently downgrade cage isolation); log-stream reframed to polled per-exec output (DEBT-119 → 11.3.B.3) since the dashboard is HTTP-only and the cage stdout is empty; lifecycle emits ride the already-async wrappers (loop-thread) with a `check_same_thread=False`+lock connection.
- Deferred: DEBT-119 (exec-log → 11.3.B.3) · DEBT-120 — retention/GC of the two append-only tables via `core/janitor.py`.

---

## 11.3: Real-time Monitoring Panels Redesign — 2026-07-22
**Status:** COMPLETE | **Gates:** tsc 0 · eslint 0 (sole pre-existing `vfs_reader.ts` warning) · npm compile 0
- Shipped: dependency-free SVG chart primitives (`RadialGauge`/`Sparkline`/`Donut` + bounded timestamped `useRingBuffer`, `format.ts`) driving redesigned Telemetry (routing donut + spend-velocity), Hardware (radial gauges + localStorage thresholds + 60s VRAM timeline), Overview (`StatTile` KPIs incl. HITL pending), and a Runtime adapter tier-resolution ladder.
- Key decision: honest-substrate frontend-only — metrics with no persisted history render as a live rolling client window (oscilloscope-style, reset on reload) rather than fabricating a series or blocking on new backend; fills animate via `stroke-dashoffset`/`transform` (not non-portable geometry-attribute transitions), a11y-labelled with reduced-motion fallbacks.
- Deferred: DEBT-116..119 — latency P50/P95, Docker Gantt, adapter tier switcher, live container logs → new sub-phase 11.3.B.

---

## 11.1: Project Context Disambiguation — 2026-07-22
**Status:** COMPLETE | **Gates:** mypy 0/423 · pytest 2434 passed + 10 new (`test_project_context_scoping.py`) · pyright 0 · npm compile 0 (tsc 0, eslint 0; sole pre-existing `vfs_reader.ts` warning) · TestClient smoke: `/api/v1/projects` + 4 scoped read filters return 200
- Shipped: persistent `projects` registry (`GET /api/v1/projects`, ghost-path-filtered) + top-bar `ProjectSelector` (localStorage + `?project_id=` URL, ghost-selection reconcile); additive nullable `project_id` column + index + PRAGMA-guarded migration on `routing_decisions`/`oom_fallback_events` (telemetry DB), `hitl_audit_log`, `dead_letter_tasks`, tagged on write from `state["project_id"]`; optional `?project_id=` read filters on telemetry/audit/DLQ endpoints; Telemetry/Overview/Audit/Recovery panels re-scope on switch; Hardware/Runtime carry an honest "machine-global" badge; BYOM/Extensions/Rules show the active-project badge.
- Key decision: audit's `project_id` is resolved at the single write-site from the **persisted checkpoint** (`brain.checkpoint.project_id_for_thread`, keyed by `session_id`==`thread_id`) rather than threaded through `request_human_approval`'s 6+ callers — reconnection-safe and low-churn; it is a plain column, never in the blake2b chain hash, so chain verification is unaffected.
- Deferred: DEBT-115 — per-project token-cost bucketing (in-memory FinOps ledger stays process-global; cards honestly badged).

---

## 11.2: GraphRAG "Neural Nebula" visualization — 2026-07-22
**Status:** COMPLETE | **Gates:** mypy 0/422 · pytest 26 (9 new memory + app-boot smoke) · pyright 0 · npm compile 0 (eslint 0; sole pre-existing `vfs_reader.ts` warning) · palette validator PASS on #000000 · `three` confirmed code-split off `main.js`
- Shipped: custom three.js 3D graph ("Neural Nebula", `panels/memory/nebula/*`) — InstancedMesh glass spheres (Fresnel + emissive-core shader), d3-force-3d one-shot-frozen layout, raycast picking, <1% breathing, search pulse over matched nodes + incident edges; the 2D ReactFlow graph is now force-directed with a pulse highlight, node `<Handle>`s (so edges actually render) and brighter strokes; the embedding vector map is a three.js points scene (density-colored via `--seq`, PCA-variance caption) that **replaced regl-scatterplot** — `regl` compiles with `new Function`, which the dashboard's `script-src 'self'` CSP forbids (this was the real "Failed to load the vector renderer" cause, surfaced once the bare `catch` was un-swallowed); new paginated/sortable File Embedding Browser with HITL-confirmed per-file purge; `ui/ConfirmModal` extracted; backend additive `/embeddings` + `/embeddings/purge` (reuses `semantic_delete`) + `max_nodes` 2000→5000; Windows `.js` MIME registration for the split chunks.
- Key decision: two engineering corrections shaped it — `react-force-graph-3d`'s per-node objects can't hold 60 FPS toward 100k so a custom InstancedMesh engine replaced it, and real glass (transmission) is un-instanceable/iGPU-costly so the crystal look is shaded; node types honestly encode only the two the file-level substrate has, and the community palette was re-validated on black (not the `#161B22` card surface).
- Deferred: DEBT-111 symbol-level node types (frozen substrate) · DEBT-112 BYOM adoption of `ConfirmModal` · DEBT-113 GPU-picking + Web-Worker layout for >tens-of-thousands nodes · DEBT-114 captured retrieval-trace for a true reasoning-path pulse.

---

## 11.0: Dashboard design system & navigation — 2026-07-20
**Status:** COMPLETE | **Gates:** npm compile 0 (tsc 0 · eslint 0, sole warning is pre-existing `editor/vfs_reader.ts`) · palette validator PASS · frontend-only (no mypy/pytest)
- Shipped: additive token layer in `shared/theme.css` (spacing/type/radius/elevation/motion/focus/z-index + dataviz-validated status/categorical-8/sequential palettes + defines the previously-undefined `--font-mono`); `dashboard/ui/` primitive set (Card/StatTile/Button/Badge/Skeleton/EmptyState/SectionHeader/ShortcutsOverlay); grouped Monitoring/Configuration/Operations nav with a persisted collapsible icon-rail (`useSidebarCollapsed`), essentials keyboard shortcuts (`useKeyboardShortcuts`: Ctrl/Cmd+B · 1–9 · ?), refined dark elevation, focus-visible rings, thin scrollbars, reduced-motion, and responsive auto-collapse.
- Key decision: the categorical chart palette was chosen by running the dataviz validator against the real `#161B22` card surface (adjacent PASS), and the validator's all-pairs FAIL on 5 hues is recorded as the reason 11.2's GraphRAG node type must encode by shape, not color — colors validated, never eyeballed.
- Fixed: undefined `--font-mono`; a duplicate conflicting `.db-btn-danger` (hardcoded `#c0392b` silently overrode the token rule); BYOM's unresolved `--vscode-*`/`--color-*` vars via a transitional compat shim (no panel rewrite).
- Deferred: DEBT-110 — migrate the BYOM panel off the `--vscode-*`/`--color-*` shim + hardcoded hex onto design tokens (owned by 11.4), plus `HardwarePanel`/`OverviewPanel` inline-hex token migration at their 11.3 redesign.
**Status:** COMPLETE | **Gates:** mypy 0/419 · pytest 2404 passed, 2 skipped (1 pre-existing FileLock flake in `test_retention.py`, unrelated — DEBT-108) · pyright 0 · new `test_context_telemetry.py` 16/16
- Shipped: `core/telemetry_log.py::log_context_utilization` (new CONTEXT category, no new sink); non-invasive instrumentation of `run_summarize_node` (rename-and-wrap with a shared-computation sink, avoiding a second tiktoken pass) and `ContextPipeline.assemble()`; `core/benchmark/session_corpus.py` synthetic long-session generator; Decision Gate recorded PROVISIONAL in `docs/PHASE_8_16_BLUEPRINT.md` pending real telemetry accrual (synthetic median 0.093 vs THRESHOLD_RATIO=0.80 is supporting characterization only, not the binding signal).
- Key decision: `session_start_time` threaded through as an additive `AIlienantGraphState` channel (§33), set once via a checkpoint-probe carry-forward resolver in `core/task_service.py` rather than reset every turn — verified via a real (non-mocked) `HybridCheckpointer.put`/`get_tuple` round trip, not assumed.
- Deferred: DEBT-108 — the one full-suite failure is a load-timing flake in the benchmark retention test (passes 3/3 solo, 2/3 in-group, and at HEAD; its stub-runner path never touches any 8.16.0 code), logged for test-hardening.
- Gate mechanism: corrected the Decision-Gate criterion (median→compaction-event-frequency; the median is thermostat-suppressed) + shipped `core/benchmark/context_telemetry_report.py` (11/11) to read real telemetry against it; verdict stays PROVISIONAL pending deferred dogfood; DEBT-109 logs the pipe-delimited→JSONL trade-off.

---

## 8.15.6: Division 8.15 checkpoint gate — 2026-07-04
**Status:** COMPLETE | **Gates:** mypy 0/417 · pytest 2389 passed, 2 skipped · pyright 0
- Shipped: `tests/test_phase8_15_checkpoint_gate.py` — 10 test-only rows re-certifying the division's cross-cutting invariants against their production entry points (extraction/shim integrity, depth+width deny-not-truncate + OOM-bounded rejection, budget reserve-deny + refund reconciliation, `analyst_readonly` floor-lock under every session mode, digest context-window ceiling, all 12 dispatch channels deserialize to safe defaults, `MAX_TOTAL_DISPATCH_FANOUT` product ceiling); no sibling suite re-run.
- Key decision: the manifest spec was stale in three ways (Division 8.15 landed 12 state channels not 4; `test_mcts_daemon`/`test_mcts_mirror` test legacy `brain.mcts.*` not the relocated tournament, whose real regression proof is the `test_subagent_tournament.py` shim; and `dispatch_depth > 2` cannot construct under the Pydantic `le=2` bound) — the gate certifies the corrected invariants and the manifest line was clarified to match.
- Deferred: DEBT-104/105/106/107 carry forward unchanged (division-closing gate opens no new debt).

---

## 8.15.5: Six-pattern dispatch wiring + guards — 2026-07-04
**Status:** COMPLETE | **Gates:** mypy 0/416 · pytest 2379 passed, 2 skipped · pyright 0
- Shipped: dynamic dispatch wired into `brain/engine.py` behind `AILIENANT_ENABLE_DYNAMIC_DISPATCH` (default off → topology-identical); admission-gate depth/width deny (bounded rejection envelopes), per-role `agent_permission` with `analyst_readonly` READ_ONLY floor-lock, budget reserve@origin/commit@synthesize, all six patterns via new `dispatch_fanout`/`dispatch_advance` nodes, `dispatch_return_node`, and `brain/dispatch_emitter.py` for planner/researcher emission.
- Key decision: shipped primitives assumed one dispatch per run + strictly-terminal synthesis; FULL scope broke both, so a consume-watermark (`_dispatch_consumed`) isolates each dispatch's results and a `dispatch_round_count` + `dispatch_advance` separates pattern rounds from wave-splitting (synthesis stays terminal-per-dispatch); admission short-circuits via a pure string router (no mixed Send/str return).
- Deferred: DEBT-106 — the 8 dev roles resolve `EDIT_EXECUTE_RBW` but run tool-less (no static arsenal builder); DEBT-107 — autonomous LLM plan-emission deferred (seam + hook shipped); DEBT-105 advanced (admission wired) but metering residue remains.

---

## 8.15.4: Budget admission ledger — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/414 · pytest 2364 passed, 2 skipped · pyright 0
- Shipped: `brain/dispatch_ledger.py` — pure `reserve_dispatch_budget`/`commit_dispatch_actual`/`refund_dispatch_reservation` (fail-closed admission, floor-at-zero refund) + `estimate_task_cost`/`estimate_wave_cost` over `estimate_iteration_cost`; `subagent_worker` now emits a real `cost_usd` (crash-safe pre-init of `loop_messages`/`trace` before the try).
- Key decision: the ledger is state-channel, not file-backed — per-task spend is already the authoritative `current_cost_usd`/`max_budget_usd` channels + checkpointer; a file would double-book. Reservation is single-flight at wave boundaries, so the functions are pure/sync (gateway's floor-at-zero discipline reused, not its FileLock). Node/edge admission wiring deferred to 8.15.5.
- Deferred: DEBT-105 — dispatch cost is estimate-based and under-counts (lenient reserve; `answer_fn`/overage unmetered); `finops`/`check_governor` remain the hard backstop.

---

## 8.15.3: Tournament module extraction — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/412 · pytest 2349 passed, 2 skipped · pyright 0
- Shipped: relocated `select_candidate_via_mcts` (+ its `_verdict_reward`/`_vfs_to_view`/`_content_to_vfs` helpers) verbatim into `brain/subagent_tournament.py::run_tournament`; `agentic_cell.py` keeps the name via a re-export shim (behavior byte-identical, existing MCTS/checkpoint-gate tests pass unmodified). New `run_tournament_from_dispatch` adapts a `DispatchBatchResult` into candidates and delegates.
- Key decision: the envelope→`{path:content}` mapping is undefined until 8.15.5, so the adapter takes a pluggable `candidate_extractor` (default = string-valued `structured_result` fields) rather than hardcoding a guess into a shipped function.
- Deferred: DEBT-104 — tournament surface rollback does not delete a candidate's newly introduced paths (contamination risk for heterogeneous dispatch candidates; warned, full isolation owned by 8.15.5).

---

## 8.15.2: Dispatch synthesis + wave batching — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/410 · pytest 2342 passed, 2 skipped · pyright 0
- Shipped: `dispatch_synthesize` node (`brain/nodes/dispatch_synthesize_node.py`) folds the accumulated `_dispatch_results` into one `DispatchBatchResult` under a per-batch char ceiling derived from the parent's `active_llm_profile` (`resolve_context_budget` × chars/token × fraction, whole-envelope greedy pack); sequential wave-splitting via a `dispatch_gate` fan-in node + `route_after_workers` loop-back + new `dispatch_wave_count` channel, capped by `MAX_CONCURRENT_SUBAGENTS` (default 4). Harness-tested; production wiring is 8.15.5.
- Key decision: a fanned-out node's conditional edge fires once PER Send instance (verified against `coder_agent`/`route_after_coder`), so the wave decision hangs off a single fan-in node (`dispatch_gate`), never off `subagent_worker` — otherwise the loop-back would re-fan N× (runaway). Winner-first ordering + `winner_task_id` await tournament selection (8.15.3); this slice is declaration-order, `winner_task_id=None`.
- Deferred: budget admission (8.15.4); six-pattern + graph wiring + feature flag (8.15.5).

## 8.15.1: Generalized Send() dispatch primitive — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/410 · pytest 2342 passed, 2 skipped · pyright 0
- Shipped: `brain/dispatch.py::build_dispatch_sends` (one `Send` per `SubagentTask`, `dispatch_depth++` at the fan-out edge, wave-slicing), `subagent_worker` node delegating to the existing `ToolDispatcher.run_loop` + a `response_schema`-constrained final answer, and the `operator.add` `_dispatch_results` fan-in channel. Compiled-harness test proves 2 concurrent Sends both write with no `INVALID_CONCURRENT_GRAPH_UPDATE` (R6); `test_swarms.py` unchanged (R1).
- Key decision: `_dispatch_results` stays pure `operator.add` (R6) and is never cleared — an `operator.add` channel cannot be reset, so `dispatch_synthesize` is terminal instead. Only `analyst_readonly`→`build_analyst_tools`; other roles run tool-less (pure-reasoning) — per-role dev arsenals + floor-lock are 8.15.5. Worker never raises: a fault becomes a `status="error"` envelope.
- Deferred: `_dispatch_results` consumers, synthesis, and waves shipped alongside in 8.15.2.

## 8.15.0: Structured dispatch schema — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/405 · pytest 2331 passed, 2 skipped · pyright 0
- Shipped: new leaf module `brain/subagent_contracts.py` (six closed-vocabulary Pydantic models — `SubagentResponseField/Schema`, `SubagentTask`, `DispatchPlan`, `SubagentResultEnvelope`, `DispatchBatchResult`) with depth `[0,2]` / width `[1,32]` / field `[1,8]` bounds, plus four additive default-safe `AIlienantGraphState` channels (`dispatch_plan`, `dispatch_batch_result`, `dispatch_depth`, `subagent_dispatch_trace`). Schema + state only — no dispatch logic or graph wiring. `tests/test_subagent_contracts.py` (14 rows).
- Key decision: two blueprint amendments in-change — `MAX_OBSERVATION_CHARS` hoisted to `shared/config.py` (single-sourced; `core/tool_dispatch._MAX_OBSERVATION_CHARS` now aliases it) so the contracts module stays a leaf and the truncation ceilings can't drift; and the SCHEMA_EVOLUTION record lands as §30, not the blueprint's reserved §27 (superseded by Division 8.14's §27–§29). Channels store `model_dump()` dicts, not models, so `state.py` imports nothing from the contracts (no cycle).
- Deferred: `_dispatch_results` fan-in channel + all dispatch mechanics → 8.15.1.

## 8.15.0.1: LLM Gateway concurrency throttle — DEBT-099 — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/403 · pytest 2317 passed, 2 skipped · pyright 0
- Shipped: a per-event-loop `asyncio.Semaphore` (`tools/llm_gateway.py::_llm_semaphore`, keyed by the running loop via `WeakKeyDictionary`) gating the five direct-call gateway methods (`ainvoke`, `astream`, `acomplete_byom`, `astream_byom`, `astream_byom_thinking`), sized by new `AILIENANT_LLM_MAX_CONCURRENCY` (default 8, floored at 1). Client-side backpressure so a fan-out is admission-controlled here, not discovered as a provider rate-limit rejection. Sibling gate `tests/test_phase8_15_0_1_checkpoint_gate.py` (THROTTLE1-5).
- Key decision: a dedicated env var, NOT a reuse of the plan-time `AILIENANT_MAX_CONCURRENT_SUBAGENTS` — a transport-layer runtime gate is a distinct enforcement layer from 8.15's wave-split ceiling. One slot per logical op: delegating methods (`acomplete_with_thinking`, `ainvoke_by_priority`) and `_oom_cascade` never re-acquire; sync `invoke` is out of scope with a bypass-DANGER warning. Head-of-line blocking under consumer backpressure accepted for honest in-flight accounting.
- Deferred: none.

## 8.14.12: Division 8.14 Checkpoint Gate amendment (polyglot round 2/3) — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/402 · pytest 2312 passed, 2 skipped · pyright 0
- Shipped: 4 new rows in `tests/test_phase8_14_checkpoint_gate.py` (POLY6, RESOLVER1-3) certifying the 8.14.10/8.14.11 widening from the division's vantage point — all 18 newly-added languages dispatch without raising, `brain.memory`'s confidence resolver and `core.blast_radius`'s traversal agree on the same dotted target, `core.dead_code`'s direct import of `blast_radius`'s private resolver names is unaffected, and a same-named file across two languages never cross-resolves.
- Key decision: amended the existing gate file rather than forking a second one — one capstone gate per division, consistent with how NOPOLLUTE1 was added at 8.14.9.
- Deferred: none.

## 8.14.11: Polyglot import extraction, round 3 (PHP, Dart) — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/402 · pytest 2308 passed, 2 skipped · pyright 0
- Shipped: two new pinned dependencies (`tree-sitter-php==0.24.1`, `tree-sitter-dart==0.1.0`), wired into `core/ast_engine.py`. New extractors for PHP (require/require_once/include/include_once + namespace `use`, separator `\`) and Dart (three resolution shapes: `dart:` built-in, `package:` URI-scheme, relative). Both node shapes verified via a live-parse spike before implementation, per the discipline used for every other language in this round.
- Key decision: `tree-sitter-dart` is a single-release package (0.1.0, no update history) — a real accepted supply-chain risk, logged as debt rather than silently absorbed. Dart's `package:foo/bar.dart` URIs are pubspec-unaware by design (stripped to a bare package-relative path, may or may not resolve) — full package-name resolution would require parsing a second file type entirely, out of scope for this round.
- Deferred: DEBT-102 (`tree-sitter-dart` single-release supply-chain risk) · DEBT-103 (Dart `package:` URI resolution is pubspec-unaware).

## 8.14.10: Polyglot import extraction, round 2 — 2026-07-03
**Status:** COMPLETE | **Gates:** mypy 0/401 · pytest 2297 passed, 2 skipped · pyright 0
- Shipped: `IMPORT_EXTRACTORS` widened from 5 to 21 languageIds (C, C++, Rust, Go, Java, Kotlin, C#, Ruby, Lua, Scala, Zig, Elixir, Haskell, Bash, PowerShell, Swift, on top of the existing Python/TS/JS/TSX/JSX) — every node shape empirically verified by live-parsing real snippets through the installed grammars before writing any extractor. New `core/module_resolver.py`: a shared, per-language-family suffix-index resolver generalizing the previously private, Python-only `blast_radius._build_python_suffix_index`, now used by both `brain.memory._resolve_edge_confidence` and `core.blast_radius`.
- Key decision: suffix indices are strictly per-family, never merged, and the AMBIGUOUS stem-collision Counter is scoped the same way — closing a dormant cross-language false-resolution bug this widening would otherwise have activated. Go resolves at directory granularity in blast-radius but deliberately stays INFERRED at the confidence-scoring layer (one target maps to many files). Python's own dotted imports now reach EXTRACTED for the first time (previously always INFERRED) — a user-visible improvement to an already-shipped path, not just new-language coverage.
- Deferred: none (PHP/Dart carved into 8.14.11 for the new-dependency risk boundary).

## 8.14.9: Division 8.14 Checkpoint Gate — Division CLOSED — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/395 · pytest 2227 passed, 2 skipped · pyright 0
- Shipped: `tests/test_phase8_14_checkpoint_gate.py` (14 rows, test-only) re-certifying the division's cross-cutting invariants from the shipped entry points — polyglot registry (5), blast-radius cycle-safety + 5K/15K/depth-3 <500 ms + to_thread offload proof, snapshot round-trip + torn-write isolation, dead-code hardcoded+JSON allowlist composition, digest bounded/token-capped/deterministic.
- Key decision: extended the gate beyond the original spec with a division-wide non-pollution row (§4 Option B, manifest-amended) — `symbol_definitions`/`boundary_edges`/`observed_call_edges` never leak into `get_all_edges`/`compute_blast_radius`; guards the physical Tier-2 separation against a future merge.
- Deferred: none.

## 8.14.8.1: Persisted observed-call-edge substrate — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/394 · pytest 2213 passed, 2 skipped · pyright 0
- Shipped: additive `observed_call_edges` Tier-2 table (`§29`) populated out-of-band by the trace harness (`persist_observed_edges`, append-only `INSERT OR IGNORE`, no lock) + a new top `OBSERVED` confidence tier merged into `find_symbol_callers`.
- Key decision: **additive, not promotion-only** — the resolver both promotes an already-found caller to `OBSERVED` *and* surfaces a pure-dynamic caller the static passes structurally cannot (the ~40% GO signal), while never demoting/removing a static candidate; AMBIGUOUS-safe via a `callee_file`∩`defined_files` match, staleness bounded at the read path (surface only if the caller's file is still indexed); `content_hash` is JSON-serialized and `project_id`-scoped to prevent cross-project `INSERT OR IGNORE` collision.
- Deferred: DEBT-101 — `observed_call_edges` has no reindex-coupled purge/TTL (orphans accumulate; bounded only at read).

## 8.14.8: Runtime-trace edge validation (SPIKE → PoC) — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/394 · pytest 2205 passed, 2 skipped · pyright 0
- Shipped: offline `core/call_trace_probe.py` harness — `sys.monitoring` (PEP 669, stdlib) intra-project call tracer + reconciler against the existing `find_symbol_callers` resolver, dogfooded over AILIENANT's own pytest; **GO** (27/67 mapped edges, ~40%, are dynamic discoveries the static resolver structurally misses — recorded in `SCHEMA_EVOLUTION.MD`).
- Key decision: no `call_edges` substrate exists to attach confidence to (§27 ships none), so this SPIKE stayed offline/additive-free — persisted `observed_call_edges` + resolver confidence-promotion deferred to a GO-gated `8.14.8.1`; found and fixed a decorator-line/`co_firstlineno` vs tree-sitter `start_line` mismatch that would have silently broken mapping for every decorated symbol.
- Deferred: DEBT-095 (polyglot/TS-JS runtime trace capture) · DEBT-096 (sandbox-execution-integrated live trace capture); the persisted substrate itself is manifest item 8.14.8.1, not a debt.

---

## 8.14.7: Cross-boundary link edges (WS / MCP seams) — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/392 · pytest 2194 passed · pyright 0 new (38 pre-existing, DEBT-094)
- Shipped: separate `boundary_edges` table (`§28`) + `core/boundary_graph.py` full-rebuild resolver + READ_ONLY `trace_cross_boundary` tool answering "what handles `server_stream_end`" across the extension/core seam, with non-pollution of code-dependency traversal structural (separate table).
- Key decision: deviated from the manifest's `brain/memory.py` per-file-extractor target to a dedicated single-flight batch builder — per-file extraction reintroduces the cross-file ordering/staleness the hybrid design exists to avoid; direction is deterministic (boundary side × channel prefix), `emits` is best-effort (typed-construction backend sends carry no literal).
- Deferred: DEBT-092 (backend `server_*` emit-site resolver) · DEBT-093 (auto-refresh on index-complete) · DEBT-094 (38 pre-existing pyright errors, separate cleanup commit).

## 8.14.6.1: Two-Tiered symbol substrate implementation — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/390 · pytest 2186 passed · pyright 0
- Shipped: `symbol_definitions` catalog (`§27`) populated off the existing tree-sitter parse
  (`core/ast_engine.py::collect_symbol_defs`), persisted via `ReactiveIndexer`; `core/symbol_refs.py`
  resolves "who calls this symbol" lazily (FTS5 narrow → AST-confirm), tagging each caller
  `EXTRACTED`/`AMBIGUOUS`/`INFERRED`; new READ_ONLY `find_symbol_callers` tool wired into both
  analyst and researcher arsenals.
- Key decision: reused the existing DEBT-041 FTS5 trigram index (`fts_narrow_catalog`) instead of a
  ripgrep subprocess — satisfies the injection-safety and search-scope-confinement conditions by
  construction, with no new dependency.
- Deferred: symbol extraction scoped to `IMPORT_EXTRACTORS`' 5 languageIds (Python/TS/JS); wider
  language coverage is a future extension, not a silent gap.

## 8.14.6: Symbol-level call-graph substrate (DECISION) — 2026-07-02
**Status:** COMPLETE | **Gates:** n/a
- Shipped: GO decision recorded in `docs/SCHEMA_EVOLUTION.MD` — a Two-Tiered Hybrid Graph (Tier 1
  file-level analytics unchanged; Tier 2 stores only symbol definitions, never call edges, resolving
  "who calls this" lazily via runtime text-search + AST validation) rather than the manifest's original
  `call_edges` sketch.
- Key decision: import-scoped resolution ranks candidates but never discards them, since a hard import
  gate would silently drop this codebase's own dynamic-dispatch callers (`core/tool_dispatch.py:205`);
  output stays `READ_ONLY`/advisory, never the sole trigger for a destructive action.
- Deferred: follow-on build carved into new manifest item `8.14.6.1`, binding eight conditions from the
  decision doc (no code lands under 8.14.6 itself).

---

## 8.14.5: Architecture-overview digest tool — 2026-07-02
**Status:** COMPLETE | **Gates:** mypy 0/388 · pytest 2170 passed · pyright 0
- Shipped: `architecture_digest` tool (`tools/perception_tools.py`) synthesizing persisted graph
  analytics into one bounded {languages, top modules, hotspots, community clusters, entrypoints,
  node/edge counts} payload via `brain/memory.build_architecture_digest_sync`; reachable by both the
  researcher and analyst (`build_researcher_tools` + `build_analyst_tools`). New `core/db` getters
  `get_all_community_ids`/`get_edge_count`; `get_top_ppr_files` given a deterministic `file_path` tie-break.
- Key decision: sources only persisted `ppr_scores`/`dependency_graph` (never rebuilds the in-RAM graph);
  the assembler stays in the picklable `brain/memory.py` by taking pre-relativized data (no `core.db`
  import); tool reachability comes from the build dicts, not the schema-only perception bundle.
- Deferred: DEBT-091 — git co-change coupling (`FILE_CHANGES_WITH`) omitted; no git-history substrate.

## 8.14.4: ADR-as-graph design spike (DECISION) — 2026-07-01
**Status:** COMPLETE | **Gates:** n/a — decision spike, no code
- Shipped: NO-GO decision on live ADR-as-graph state recorded in `SCHEMA_EVOLUTION.MD`; spike closed.
- Key decision: reject an `architecture_decisions` table + `REFERENCES` edges — the need is already met by `AILIENANT.md` standing guidance and the analyst docs/codex brain, and GO would fight the timeless-documentation invariant plus add a stale-edge surface.

## 8.14.3: Dead-code detection (analyst tool) — 2026-07-01
**Status:** COMPLETE | **Gates:** mypy 0/387 · pytest 2152 passed · pyright 0
- Shipped: `core/dead_code.py` — file-level zero-resolved-in-degree, non-entrypoint orphan scan
  over `dependency_graph`, with a hardcoded entrypoint set plus `.ailienant/dead-code-allowlist.json`
  glob extension; `detect_dead_code` analyst tool in `tools/analyst_tools.py`.
- Key decision: scope corrected to file-level (not symbol-level) per 8.14.6's own documented
  "near-inert without a call-graph" framing; in-degree is resolved (not the raw dashboard
  aggregate) to avoid false orphans on dotted-module imports; all content reads run inside the
  thread-pool compute, narrowed to already-filtered candidates, via a size-capped jailed reader.

## 8.14.2: Shared memory snapshot export/import — 2026-07-01
**Status:** COMPLETE | **Gates:** mypy 0/385 · pytest 2121 passed · pyright 0
- Shipped: `core/memory_snapshot.py` — portable export/import of a project's `dependency_graph` + PPR
  analytics to a committed `.ailienant/memory.db.zst`, with a session-init bootstrap that warm-starts
  the graph before the full crawl; `bulk_import_graph` in `core/db.py`; additive
  `client_export_memory_snapshot` WS command.
- Key decision: the snapshot is path-relative and project-agnostic (relativize + re-key on import) so a
  committed artifact works across clone paths; graph+PPR only (not `indexed_files`) so the crawl still
  builds the vector store; bounded streaming decompression caps a zip bomb; file-backed temp SQLite
  avoids the optional `sqlite3_(de)serialize` C API for portability.
- Deferred: DEBT-090 — extension-side export button/command palette entry (backend command is wired).

## 8.14.1: Git blast-radius mapper (pre-apply validator) — 2026-07-01
**Status:** COMPLETE | **Gates:** mypy 0/383 · pytest 2110 passed · pyright 0
- Shipped: `core/blast_radius.py` — a resolved reverse-adjacency BFS over `dependency_graph` computing
  transitive dependents of a pending diff's changed files (cycle-safe, deterministic, off-loop via
  `asyncio.to_thread`), wired as a pre-apply gate in `task_service.py` that escalates to human review
  via `request_human_approval("BLAST_RADIUS", …)` when the radius exceeds `BLAST_RADIUS_THRESHOLD_FILES`
  (env-configurable, default 25) and vetoes the write on decline.
- Key decision: deviated from the manifest's literal "reuse `bfs_k_hop_backward`" — that SQL walker
  seeds on raw node strings, but post-8.14.0 a dependent references a changed file by import specifier
  (extensionless TS/JS path or dotted Python module), not its absolute path, so it silently
  under-counts. Built a resolved in-memory reverse adjacency instead, sharing the confidence resolver
  (extracted as `resolve_target_to_file`) plus a fail-safe Python suffix index (over-count, never
  under-count — the safe direction for a review gate). A mapper fault fails open (advisory); a
  threshold breach fails closed.
- Deferred: DEBT-088 — `bfs_k_hop_backward` has the same resolved-form gap; DEBT-089 — Python
  resolution is suffix-based, not sys.path-aware.

---

## 8.14.0: Polyglot dependency extraction (IMPORT_EXTRACTORS registry) — 2026-07-01
**Status:** COMPLETE | **Gates:** mypy 0/381 · pytest 2095 passed · pyright 0
- Shipped: `language_id`-dispatched `IMPORT_EXTRACTORS` registry (Python refactored verbatim + TS/JS
  static/re-export/dynamic-`import()`/`require()`), lexical disk-free relative-specifier resolution with
  a strict workspace-boundary guard, and extension/`index.*` candidate expansion in the confidence
  resolver — the dependency graph is now polyglot rather than silently Python-only. Closes DEBT-080.
- Key decision: relative resolution is `posixpath`-based on forward-slashed input (deterministic on a
  Windows host vs. a Linux worker); the guard is a directory-segment boundary, not a naive prefix.
- Deferred: DEBT-087 — Python relative imports (`from .mod import x`) still skipped, asymmetric with TS/JS.

## 8.13.6: Division 8.13 Checkpoint Gate — CLOSED — 2026-06-30
**Status:** COMPLETE | **Gates:** mypy 0/380 · pytest green (8/8 gate rows) · pyright 0 · npm compile 0
- Shipped: `tests/test_phase8_13_checkpoint_gate.py` (8 rows) certifying the division's cross-cutting invariants — oracle cage untouched, untrusted/session-less execution never reaches the devcontainer, the trusted tier's fallback targets Native (never the cage), every pre-execution failure delegates while mid-execution failures degrade in place (idempotency), a hanging bridge is bounded, and the WS contract is additive and tolerant. **Division 8.13 (Polyglot Devcontainer Execution Layer) is now CLOSED.**
- Key decision: auditing division closure surfaced that DEBT-035 (untrusted MultiPL-E TS execution) is **not** resolved by 8.13 — it is the opposite threat model (§2) and the benchmark lane permanently stays `unsupported_runtime`; only DEBT-082 was resolved. Corrected the manifest/backlog to avoid a false resolution claim.
- Deferred: DEBT-035 remains open (needs a distinct locked Node-capable tier, no phase yet); DEBT-083–086 from 8.13.5 remain open.

---

## 8.13.5: Trusted-tier wiring + concrete host bridge + Selective HITL Fallback — 2026-06-30
**Status:** COMPLETE | **Gates:** mypy 0/379 · pytest 2071 passed (2 skipped) · pyright 0 · npm compile 0 · npm lint 0
- Shipped: end-to-end trusted devcontainer execution — `api/devcontainer_bridge.py` (`WebSocketHostBridge` over the ConnectionManager primitives, injectable manager), `main.py` receive-loop dispatch + composition-root bridge injection (`set_trusted_bridge`, DI — `core` imports no transport layer), `core.sandbox.resolve_execution_adapter` chokepoint wired at the 3 live run_command sites (coder, tracked-tool, `sandbox_bash`); extension host handler (`devcontainerExecHandler.ts`), `contracts.ts` 5 events, `AILIENANT: Scaffold devcontainer` command. New tests: bridge (5) + adapter fallback/selection (7) + host handler (6).
- Key decision: **Selective HITL Fallback** — an unavailable devcontainer (pre-execution: no bridge / provision fail / no `devcontainer.json`) delegates to the HITL-gated `NativeHITLSandboxAdapter` (propose→consent→host-native), never the untrusted cage; a mid-execution failure degrades in place (idempotency). Reuses the existing Native tier (no new subsystem).
- Deferred: DEBT-083 (incremental exec streaming), DEBT-084 (interactive sessions over the bridge), DEBT-085 (sub-cwd→container mapping), DEBT-086 (typecheck/validation-helper routing).
**Status:** COMPLETE | **Gates:** mypy 0/377 · pytest 2059 passed (2 skipped) · pyright 0 · npm compile 0 · npm lint 0
- Shipped: 5 additive devcontainer WS events in `api/ws_contracts.py` (provision request/status + exec request/stream/exit, `request_id`-correlated, env **names-only**) + `ConnectionManager` transport primitives (emit/wait/resolve, terminal-only provision resolve, disconnect-reaping) in `api/websocket_manager.py`; `SCHEMA_EVOLUTION.MD §26`; `tests/test_devcontainer_ws_contract.py` (7 rows). Contract + transport only — receive-loop dispatch + concrete bridge wire in 8.13.5. Boy-Scout: fixed the `ws_contracts.py` header + translated adjacent Spanish log strings.
- Key decision: DEBT-082 resolved via the host-prerequisite CLI model — `@devcontainers/cli` moved to a dev-only `devDependency` (never shipped in the `.vsix`), the CLI is sourced from PATH / Dev Containers ext, and the provisioner degrades with an actionable remediation; chosen over bundling per §9 (no supply-chain bloat).
- Resolved: DEBT-082 — host-prerequisite distribution model ratified and documented.
**Status:** COMPLETE | **Gates:** npm compile 0 · npm lint 0
- Shipped: `devcontainerProvisioner.ts` (vscode-free DI core — PATH→ext→optional-dep probe, lazy single-flight `up()`, SIGTERM→SIGKILL grace, 10 min timeout-degrade); `devcontainerFactory.ts` (vscode wiring + lazy singleton); wired into `extension.ts`; `@devcontainers/cli` pinned optional dep + esbuild external; `RuntimePanel.tsx` honest scaffold card; 10 mocha unit tests passing.
- Deferred: DEBT-082 — `@devcontainers/cli` not shipped in the `.vsix` (`.vscodeignore` excludes `node_modules`); packaged extension relies on PATH / Dev Containers ext until 8.13.4 resolves the distribution model.

---

## 8.13.2: DevcontainerSandboxAdapter — trusted-tier backend over host bridge — 2026-06-30
**Status:** COMPLETE | **Gates:** mypy 0/376 · pytest 2053 passed (2 skipped) · pyright 0
- Shipped: `DevcontainerSandboxAdapter` in `core/sandbox.py` — a thin trust-tier router that delegates to a new `HostExecutionBridge` Protocol seam; lazy single-flight provisioning, DLQ-on-timeout, never-crash degrade mirroring NativeHITL; `tests/test_devcontainer_adapter.py` (12 rows). Boy-Scout: explicit `import docker.errors` clears 2 pre-existing pyright stub errors.
- Key decision: the adapter is inert w.r.t. the safety resolver (no `ACTIVE_TIER`/`get_active_adapter` change) so the untrusted benchmark oracle keeps its locked Docker cage, and interactive sessions delegate to the bridge rather than building a throwaway PTY-over-WS backend.
- Deferred: DEBT-035 — host bridge + tier-selection wiring still pending (8.13.3–8.13.5); the adapter degrades to `[devcontainer_bridge_unavailable]` until they land.

---

## 8.13.1: Polyglot Devcontainer Execution Layer — Blueprint + ADR ratified — 2026-06-29
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: `docs/PHASE_8.13_BLUEPRINT.md` ratified (ADR-762); split-by-trust invariant, extension-owned lifecycle, host-bridge contract (+ non-normative wire sketch), CLI probe/degrade order, trusted-tier security model, and §9 soft-dep justification frozen binding; manifest header + overview tagged [ADR-762].
- Key decision: the devcontainer tier serves only *trusted* project execution; the untrusted benchmark oracle keeps its locked Docker cage (Charter §4 invariant preserved, not dissolved).
- Deferred: DEBT-035 — resolved across 8.13.2–8.13.6 (adapter + host-bridge + wiring + gate); this sub-phase freezes the contract only.

---

## 8.10.25: Workspace.tsx store-backed WS controller extraction — 2026-06-29
**Status:** COMPLETE | **Gates:** npm compile 0 (check-types 0 · lint 0 errors · esbuild ok)
- Shipped: Memory-only `useChatStore` (22 live fields + `hydrate()`); 45-branch WS dispatch extracted to `useWSMessageHandler()` (no-arg, `getState()` synchronous truth, once-registered listener, rAF buffers, stall watchdog); session transcript effects in `useSessionPersistence()`; `ToastStack.tsx` component; `types.ts` + `utils/messageDispatchHelpers.ts`; `Workspace.tsx` 1981 → 726 lines.
- Key decision: new memory-only `useChatStore` (vanilla `create`, not `createPersistedStore`) keeps live state out of `vscode.setState` — persisting the transcript on every token would blow the setState quota and duplicate PERSIST_TRANSCRIPT; all switch reads use `getState().messages` (synchronous truth) eliminating the 1-tick render-cycle lag `messagesRef` had under WS bursts.

---

## 8.10.24: STATE_COMPACTED chip + streaming footer aria-live — 2026-06-29
**Status:** COMPLETE | **Gates:** npm compile 0 (check-types 0 · lint 0 errors · esbuild ok)
- Shipped: `Message` refactored into a discriminated union (`ConversationMessage | SystemMessage`) with `streaming?: never; toolCalls?: never` on the system arm to satisfy generic constraints; `state_compacted` WS handler pushes a `SystemMessage` chip with a streaming-tail guard; chip renders via early JS return before ErrorBoundary (bypasses the full row structure); filtered from PERSIST_TRANSCRIPT via type-predicate `.filter((m): m is ConversationMessage => m.role !== 'system')`; `aria-live="off" aria-atomic="true"` added to streaming token footer; `.ws-system-chip` CSS follows the `.ws-thinking` inline-trace pattern.
- Key decision: toast stack container did NOT receive `aria-live` — individual `role="alert"` children already create assertive live regions; adding a polite container live region would cause double-reads in NVDA/VoiceOver/JAWS.

---

## 8.10.23: Webview React error boundaries — 2026-06-29
**Status:** COMPLETE | **Gates:** npm compile 0 (check-types 0 · lint 0 errors · esbuild ok)
- Shipped: new reusable `src/workspace/components/ErrorBoundary.tsx` (class boundary, node-or-render-prop `fallback`, `resetKeys` auto-clear, console diagnostic); a root catch-all in `main.tsx` (`WorkspaceCrashPanel` with Try-again/Reload actions) and a per-message-row boundary in `Workspace.tsx` so one malformed turn degrades to an inline notice instead of blanking the transcript; row key switched from array index to `m.id ?? row-${i}`.
- Key decision: the root boundary wraps `<Workspace>` at the `main.tsx` mount point (not inside its return) so a throw in Workspace's own render body is also caught; per-row isolation is the primary value (whole transcript stays mounted), the root panel is the last resort — boundaries catch render faults only, not the WS reducer's event-handler throws.

## 8.10.22: Host logger + console migration — `shared/logger.ts` — 2026-06-29
**Status:** COMPLETE | **Gates:** npm compile 0 · npm lint 0 errors
- Shipped: filled the 0-byte `src/shared/logger.ts` with a lazy "AILIENANT" output-channel logger (`debug/log/warn/error`, Error-stack + JSON arg formatting); migrated all 13 bare `console.*` calls across the 7 host modules (`extension.ts`, `ws_client.ts`, `workspace_provisioning.ts`, `brain/session.ts`, `providers/workspace_panel.ts`, `providers/mirror.ts`, `api/api_client.ts`) to it. Webview/React `console.*` left out of scope.
- Key decision: `logger.ts` lives in `shared/` but is host-only (imports `vscode`); safe because all 7 targets already import `vscode`, so webview-bundle reachability is unchanged. Boy-scout: translated Spanish comments/log strings and fixed a stray bare-string statement at `api_client.ts:1`.

## 8.10.21: Typed WS contract layer — `api/contracts.ts` — 2026-06-29
**Status:** COMPLETE | **Gates:** npm compile 0 · npm lint 0 (no new errors)
- Shipped: filled the 0-byte `src/api/contracts.ts` with the full wire union mirroring backend `ws_contracts.py` — 58 `event_type`-discriminated variants (35 server→client `ServerWSMessage`, 23 client→server `ClientWSMessage`, + `WSMessage` alias and an `isServerEvent` guard); typed `SessionManager._onWSMessage` against `ServerWSMessage` with a single boundary cast at the `onMessage` registration. Runtime no-op.
- Key decision: membership is split by message origin, not the `event_type` string prefix — `state_compacted` is a server event without the `server_` prefix, so it is enumerated explicitly in both the union and the guard set.

## 8.10.20: Benchmark artifact retention — DEBT-039 — 2026-06-29
**Status:** COMPLETE | **Gates:** mypy . 0/375 · pyright 0 · pytest 2041 passed, 2 skipped
- Shipped: configurable max-artifacts cap (default 20) with LRU-by-mtime eviction at the write site — `prune_artifacts` in `core/benchmark/report.py` and `_persist_with_retention` in `core/benchmark_service.py`, which reads `benchmark.max_stored_runs` from the global `~/.ailienant/.ailienant.json`; new gate `tests/benchmark/test_retention.py` (19 tests).
- Key decision: serialize write+prune under an in-process `asyncio.Lock` + cross-process `filelock.FileLock` with all blocking I/O on `asyncio.to_thread` (mirrors `docs_index`), and write durability-first on a lock timeout — a completed report is never lost to cleanup-lock contention.
- Deferred: none — DEBT-039 closed.

## 8.10.19: brain/ strict-mode typing pass — DEBT-005 — 2026-06-29
**Status:** COMPLETE | **Gates:** mypy brain/ --strict 0/33 · mypy . 0/374 · pytest 3 passed
- Shipped: Cleared 2 strict errors in `brain/agentic_cell.py`: removed stale `# type: ignore[union-attr,index]` on LLM response access (line 142); added scoped `# type: ignore[attr-defined]` on `from core.permissions import` block (line 862) for `PermissionMode` re-exported without `__all__`. Boy-scout: translated Spanish section headers and stripped Phase PM references in `brain/engine.py`.
- Key decision: strict surface was in `agentic_cell.py`, not `engine.py` as the debt entry anticipated — engine.py, context_pipeline.py, summarizer.py, and agent_context.py were all clean under `--strict --follow-imports=silent`.

## 8.10.18: Live STATE_COMPACTED emission — DEBT-076 — 2026-06-28
**Status:** COMPLETE | **Gates:** mypy 0/374 · pytest 2022 passed
- Shipped: `functools.partial(vfs_manager.broadcast_state_compacted, session_id)` injected into `cfg["configurable"]["on_state_compacted"]` in `core/task_service.py`; `brain/summarizer.run_summarize_node` gains an optional `config` param and calls `_emit_compacted` (fire-and-forget, BLE001-guarded) after both successful LLM compression and the bare-except truncation fallback. Gate `test_phase8_12_4_checkpoint_gate.py` (SC1/SC2/SC3) certifies live wiring, arity contract, and threshold-silent path.
- Key decision: callback threaded via `RunnableConfig.configurable` (same seam as `narrate`/`stream_thinking`) so `vfs_manager` stays out of the `brain/` import graph and the summarizer remains testable with a spy; VRAM-cancelled early-return does not emit (user-initiated cancel, not engine compaction).

## 8.10.17: Unify analyst budget onto ContextPipeline — DEBT-077 — 2026-06-26
**Status:** COMPLETE | **Gates:** mypy 0/373 · pyright 0 (prod) · pytest 2019 passed
- Shipped: `assemble_analyst_context` now routes its sources through the shared `build_agent_context` (CODEX→Foundation, README+GraphRAG→Project, docs+active-file→Execution); the bespoke `ContextBudgetManager` tier-ladder packer + soft-cap constants are deleted, a `ContextBudgetError` path drops the Project layer wholesale on overflow, a `_G3_OVERHEAD_TOKENS` reserve keeps the post-assembly raw-data clause within the tier budget, and a G3 repair guard re-appends the file block's closing boundary tag if Execution-layer truncation cuts it.
- Key decision: the pipeline has no soft-cap layer, so anti-starvation is replaced by "pinned L1-L3 + degrade"; the single active-file region keeps one boundary tag pair (single-file `path=` attribute form preserved for the existing G3 sandbox tests) so truncation can corrupt at most one trailing tag.
- Deferred: DEBT-081 — the empty Conversation (L4) layer reserves 2/3 of the post-foundation budget, under-filling the single-shot analyst and squeezing file+docs into the L5 third.

## 8.10.16: HITL Restart-Durability — DEBT-072 — 2026-06-24
**Status:** COMPLETE | **Gates:** mypy 0/373 · pyright 0 · pytest green (gate 5 rows + checkpoint/session/dlq/resume suites)
- Shipped: `HybridCheckpointer.recover()` now re-seeds `hybrid_writes_l2` pending writes (incl. a paused `interrupt()`) via `put_writes`, so a HITL approval suspended before a restart survives it; `promoted_at` switched from `time.monotonic()` to `time.time()` (+ `checkpoint_id` tie-break) so cross-restart ordering can't resurrect a stale interrupt; `write_idx` enumerated to stop multi-write PK collisions; `arecover`/`apromote` async offload wrappers added and routed at the DLQ-resume + interrupt-promote sites; `task_service.rehydrate_paused_interrupt` re-arms `_paused_tasks` and re-emits the card on session reopen.
- Key decision: the durable security posture (`session_permission_mode`) is read back from the recovered checkpoint and seeded into the resume-branch state — closing the out-of-graph MCP-gate "DEFAULT downgrade" for both cross-restart and in-process resumes — with no new L2 schema and no `TaskPayload` serialization (FinOps/secrets-hygiene preserved).
- Deferred: DEBT-079 — exact original `TaskPayload`/thinking-config fidelity on a cross-restart resume (reconstructed-minimal payload is the declared MVP).

## 8.12.4: Division 8.12 Checkpoint Gate — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/372 · pytest 2014 passed · pyright 0
- Shipped: `test_context_pipeline.py` (16 tests, test-only) locks the division invariants — L1-L3 never evicted (hard `ContextBudgetError` only), L4 FIFO drops oldest in order, `on_compacted` fires once on eviction and is silent otherwise, L5 tail-truncation stays token-exact within budget, plus the `broadcast_state_compacted` wire-event shape via a hermetic stubbed manager. Closes Division 8.12.

## 8.12.3: STATE_COMPACTED wire contract — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/372 · pytest 2014 passed · pyright 0
- Shipped: documented the already-coded `state_compacted` server event in `SCHEMA_EVOLUTION.MD §25` (+ §17 event list); ratified the `summary → compaction_message` field rename (system status line, not AI prose).
- Deferred: DEBT-078 — frontend contract mirror + Phase 11.7 `SessionSummaryCard` consumer (extension `contracts.ts` has no server-event union yet).

## 8.12.2: Agent integration — context budget-guard — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/372 · pytest 2014 passed · pyright 0
- Shipped: `brain/agent_context.py` (`build_agent_context` + shared `resolve_context_budget`/`AMNESIA_ALERT`); planner and coder now route their durable context (identity/rules/memory) and volatile IDE content through the pipeline so L5 is trimmed first and L1-L3 are never silently dropped.
- Key decision: focused budget-guard (not full pipeline ownership) — agents keep their boundary-tag sandbox and response-cache keys; the resolved budget is folded into the cache key so a local↔cloud reroute can't serve a stale trim. On budget exhaustion the node degrades to identity-only plus an amnesia alert rather than crashing.
- Deferred: DEBT-076 (live STATE_COMPACTED emission from the conversation-accrual path) · DEBT-077 (unify analyst `ContextBudgetManager` onto the pipeline).

## 8.12.1: ContextPipeline — 5-layer context assembler — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/370 · pytest 1998 passed · pyright 0
- Shipped: `brain/context_pipeline.py` — `ContextChunk` (moved from `agents/`), `ContextLayer` ABC, 5 concrete layers (Foundation/Project/Memory/Conversation/Execution), `ContextPipeline` with dynamic budget (L1-L3 anchor; safety buffer; L4 FIFO batch-eviction; L5 token-exact tail-truncation), `ContextAssemblyResult` observable return; `broadcast_state_compacted()` added to websocket_manager via `StateCompactedPayload`.
- Key decision: `ContextChunk` moved to `brain/` (not `agents/`) so `brain.context_pipeline` is a foundation-layer import; agents/ imports brain/, never the reverse — circular import eliminated structurally.

## 8.11.5: YOLO Guard + Matrix Combined Gate — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/369 · pytest 1998 passed · pyright 0 · npm compile 0
- Shipped: `test_yolo_guard_integration.py` asserts the composed `evaluate_action → risk_intercept_guard` pipeline across all 7 modes × risk categories — locking no-double-interception in the 5 non-permissive modes, ALLOW→HITL upgrade in FULL_AUTO/STANDARD, and legacy-alias dormancy. Closes Division 8.11.
- Key decision: the gate runs the *composed* pipeline (matrix verdict then content post-filter) rather than the guard in isolation, so the short-circuit that prevents an amber RISK_INTERCEPT card in "Ask" mode is verified end-to-end.

## 8.11.4: Division 8.11 Checkpoint Gate — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/369 · pytest 1998 passed · pyright 0 · npm compile 0
- Shipped: `test_permission_modes.py` locks the full 7-mode × 4-tier decision surface against an independent contract table transcribed from SCHEMA_EVOLUTION §23, plus identity floor, ASK==HITL, legacy-migration equivalence, and wire-value round-trip (171 cases with 8.11.5).
- Key decision: the contract table is hand-transcribed and deliberately NOT imported from `_DECISION_MATRIX`, so source and gate must agree — a one-sided edit fails the gate.

## 8.11.3: Shadow Mapping + YOLO Guard — 2026-06-23
**Status:** COMPLETE | **Gates:** mypy 0/367 · pytest 1827 passed · npm compile 0
- Shipped: `_FRONTEND_MODE_TO_SESSION` now targets canonical modes (`automatic→STANDARD`, `ask_before_edits→CAUTIOUS`, `plan_mode→PLAN_ONLY`); `risk_intercept_guard()` upgrades ALLOW→HITL for 5 risky command categories in FULL_AUTO/STANDARD sessions; `RISK_INTERCEPT` HITL card variant in `HITLInterventionCard.tsx`; 55-case `test_yolo_guard.py`; SCHEMA_EVOLUTION §24.
- Key decision: YOLO Guard is a per-call post-filter only — it never mutates session mode and never fires in modes (CAUTIOUS/ASK_EXECUTE/ASK_ALL) where the matrix already gates commands through HITL, avoiding double-interception.
- Deferred: DEBT-073 — 4× `"plan_mode"` literal in `Workspace.tsx` (DRY) (UI unchanged this sub-phase, no real duplication today).

---

## 8.11.2: evaluate_action 7×3 Resolver Rewrite — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/366 · pyright 0 · pytest 1772 passed
- Shipped: canonical-native `evaluate_action` over an authoritative 7×3 `_DECISION_MATRIX` with legacy normalization via `_LEGACY_MODE_MIGRATION`; identity floor preserved; signature unchanged so all consumers untouched. Seed allowlist in `core/task_service.py` widened to all valid modes; SCHEMA_EVOLUTION §23.
- Key decision: `FULL_AUTO×DANGEROUS=ALLOW` (sole unprompted-irreversible mode) and `CAUTIOUS×WRITE=HITL` (faithful target of legacy DEFAULT); `gateway/governance.py` audited and confirmed unchanged.

---

## 8.11.1: 7-Mode session_mode Enum Extension — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/366 · pyright 0 · pytest 1753 passed
- Shipped: additive `SessionPermissionMode` 7-mode vocabulary + 3 deprecated legacy aliases; widened `session_permission_mode` state Literal; SCHEMA_EVOLUTION §22; behavior-inert (resolver is 8.11.2).
- Key decision: behavior-faithful legacy migration (DEFAULT→CAUTIOUS, AUTO→STANDARD, PLAN→PLAN_ONLY) — not the manifest's literal DEFAULT→STANDARD, which would silently loosen existing strict sessions.

---

## 8.10.15: Pyright Typing Pass — DEBT-071 retired — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/366 · pyright 0 · pytest 1690 passed
- Shipped: 14 `# pyright: ignore[reportArgumentType]` on `brain/engine.py` `add_node` calls; 47 `# pyright: ignore[reportIncompatibleVariableOverride]` on `args_schema` overrides across 13 `tools/*.py` files; stale DLQ comment corrected; pre-existing `mcp_adapter.py` `reportGeneralTypeIssues` suppressed (Boy Scout).

---

## 8.2.6.5: Division 8.2.6 Checkpoint Gate — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/366 · pyright 0 · pytest 1690 passed
- Shipped: `tests/test_phase8_2_6_warmup_gate.py` — 8-row sibling gate certifying A1/A2 corpus-presence probe, B1 empty-corpus routing (LOCAL_SMALL + is_red_alert=False), B2 non-empty css<40 regression guard (CLOUD), C1 cold-store zero-embed assert, D1/D2 warm-up defer/run, E1 single-retry-then-re-raise; all rows isolated and hermetic.

## 8.2.6.4: Mid-session local-endpoint failover — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/365 · pyright 0 new (10 pre-existing union-type) · pytest 1682 passed
- Shipped: new `model_resolver.get_failover_target(tier, exclude_model)` walks the capability ladder nearest-first for the next callable target; `acomplete_byom`/`astream_byom` fail over once on a non-OOM `APIConnectionError` from a local endpoint, leaving OOM-class drops to the existing cascade and re-raising on a second failure or when no viable neighbour exists; 11 hermetic tests cover resolution, drop-then-recover, persistent-drop-no-loop, and OOM/cloud exclusion.
- Key decision: streaming failover binds to the initial connect only (pre-first-yield) since a partially streamed answer cannot be re-rolled; `astream_byom_thinking` left untouched per strict DoD scope.

## 8.2.6.3: Warm-up indexing gate — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/364 · pyright 0 · pytest 1671 passed
- Shipped: `_WARMUP_MIN_FILES = 5` constant in `core/indexer.py`; `LazyIndexer._run` defers the full crawl when `0 < total < _WARMUP_MIN_FILES` — fires `complete_event` and `broadcast_indexing_complete` but leaves `_is_complete = False` so the next session retries when the workspace grows; 2 hermetic async tests assert sub-threshold defers and at-threshold runs; Boy-Scout: stale phase reference scrubbed from `_preflight_check` docstring.

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

## 11.5.B: Coder Companion — Structured Post-Turn Explanation — 2026-07-25
**Status:** COMPLETE | **Gates:** mypy 0/437 · pytest 22 new (86 in coder sweep) passed · pyright 0 · npm compile 0
- Shipped: `brain/coder_companion.py` fire-and-forget explanation pass triggered from `run_coder_node`, emitting `ServerCoderCompanionEvent` (objective/decisions/patterns/bottlenecks/security_notes/errors/follow-ups) rendered by new `CoderCompanionCard.tsx` beside the diff-approval surface.
- Key decision: reuses the existing droppable-side-channel idiom (strong-ref task set + done-callback, dual narrow exception guards, explicit LLM timeout, concurrency semaphore) rather than adding a blocking graph node; verbosity resolved from structural state signals, `reasoning_summary` schema-present but unpopulated.
- Deferred: DEBT-121 — non-blocking local-tier VRAM-lock probe for `_companion_gpu_slot_available` (MVP unconditionally admits).

## 12.1: Cacheable System-Prompt Prefix (prerequisite for provider caching) — 2026-07-31
**Status:** COMPLETE | **Gates:** mypy 0/450 · pytest 2664 passed, 2 skipped · pyright 0 · npm compile 0
- Shipped: measurement showed 12.1's original "high-volume prefix" premise didn't hold (the real prefix is ~281-450 tokens, below every provider's cacheable floor), so scope narrowed to the actual blocker — the per-turn sandbox nonce interpolated into the system prompt's axiom text. New `agents/prompts.py::build_static_identity_prompt`/`build_boundary_declaration` split the system message into a byte-identical HEAD and a small per-turn TAIL, wired into `agents/planner.py` and `agents/coder.py`, including the `ContextBudgetError` degrade paths.
- Key decision: rejected relocating the nonce declaration to the user turn (review flagged it as a new prompt-injection vector — untrusted content could forge a competing declaration in the same message role); kept it exclusively in the system role instead.
- Deferred: DEBT-137 — provider `cache_control` + cache telemetry, unblocked by 12.7's coder tool-calling.

## 12.3: Remaining Integration DEBTs Sprint — 2026-08-01
**Status:** COMPLETE | **Gates:** mypy 0/451 · pytest 2677 passed, 2 skipped · pyright 0 · npm compile 0 · npm test 152 passed
- Shipped: corrected both DEBT entries rather than implementing their literal (and inaccurate) prescriptions. DEBT-049's premise was false — the resolver's default-embedder fallback already existed; corrected `SkillInvokeTool`'s docstring and `core/tool_registry.py`'s false "gateway duplicate" exclusion reason for 4 tools (real reason: role-scope disjointness from `resolve_tools()`'s only consumer). DEBT-054 was half-closed already (8.18 wired `todo_write`'s dispatch); closed the remainder — `core/tool_dispatch.py::promote_tool_state` folds the tool's payload into the `agent_todos` channel, broadcast via new `server_agent_todos` WS event to a new `AgentTodoPanel.tsx`.
- Key decision: did not add a `skill_invoke` factory (would be unreachable dead code — its allowed roles never overlap the agentic cell's coder-only role set); event-loop parse ceiling (`MAX_JSON_PARSE_CHARS`) checked before `json.loads`, and WS emission suppressed on a value-equal re-write, per pre-implementation security review. Fixed an incidental latent bug: `ToolDispatcher.dispatch` bypasses LangChain's arg validation, so `todo_write` would `AttributeError` on any real (non-empty) call before this fix.
- Deferred: none — a new DECISION-record correction was folded into the existing DEBT-131 entry instead of a new DEBT.

## 12.4: Devcontainer Follow-Ups (orphaned by 8.13 closure) — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/453 · pytest 2702 passed, 2 skipped · pyright 0 · npm compile 0 · npm test 182 passed
- Shipped: DEBT-083 (host-side `onChunk` streaming + coalescer), DEBT-085 (two-tier container-root resolution + confined `cwd` translation), and DEBT-086 (`interactive_fallback=False` non-interactive trusted routing for `check_type_integrity`/`coder_tools._exec`) closed in full; DEBT-084 shipped as the §43 interactive-session WS tunnel (bidirectional backpressure, cancellation-safe teardown, new `core/command_boundary.py` shared with `core.pty_session`, host driver `devcontainerSessionHandler.ts`) with the agentic-cell reroute deliberately deferred.
- Key decision: architect review during planning rejected a naive session-id thread-through for DEBT-086 (would have double-prompted HITL) and split DEBT-084 into tunnel-plumbing-now vs. cell-consumer-later, gated on an OCC-safe sync surface that doesn't exist yet — a raw bind-mounted `SyncSurface` would bypass the VFS barrier's stale-guard.
- Deferred: DEBT-138 — reroute `brain/agentic_cell.py` to the devcontainer tier, blocked on the OCC-safe sync surface; DEBT-139 — no real TTY in the session host driver (no `node-pty`, by design).

## 12.11: GraphRAG Retrieval Fidelity — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/455 · pytest 2713 passed, 2 skipped · pyright 0
- Shipped: closed DEBT-141 (embed-truncation now logged with real counts; ceiling resolved per-provider via new `EmbeddingTarget.max_input_tokens` instead of a fixed constant), DEBT-142 (`search_snippets` distills a whole-file AST skeleton instead of returning the stored 500-char audit slice as RAG evidence, wired through all four production consumers including the MCP `query_memory` tool), and DEBT-143 (`deep_parse`'s uncapped read/parse loop now caps on real content tokens + PPR-ranked neighbors; its dead, zero-caller sibling `extract()`/`ExtractionResult`/`_apply_guardrails` removed).
- Key decision: the DEBT-143 cap applies to the read/parse loop only, never to `target_files` itself — `coverage_ratio`'s denominator stays the pre-cap set, since an earlier draft that shrank it first would have inflated `graph_coverage` (0.3 weight of CSS) exactly when context was truncated; caught in review before implementation.
- Deferred: DEBT-140 (symbol-level chunk embeddings, 12.13) and DEBT-144 (delete dead `brain/prompt_builder.py`, 12.12) — both registered, neither blocking this closure.

## 12.12: Dead Context-Assembler Reclamation — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/454 · pytest 2724 passed, 2 skipped · pyright 0
- Shipped: closed DEBT-144 — deleted the fully dead `brain/prompt_builder.py` (~330 lines, zero production callers, harvested by 12.11 before removal). Auditing the spec before executing it surfaced a two-level orphan cascade the entry hadn't named: `brain/orchestrator.py` (sole caller was `prompt_builder.py`) and `LazyIndexer.progress_percentage` (sole consumer was `orchestrator.py`) both went with it; verified the live IDE progress bar computes its own percentage independently, so nothing regresses. Scrubbed three stale basename-collision comments (`mypy.ini`, both package `__init__.py` markers) and two doc-comments naming the deleted module.
- Key decision: deleted the whole cascade in one pass rather than deleting only the named file and re-logging the rest — the capability `brain/orchestrator.py` provided was already dark in both halves (its own prefix, and the parallel `is_indexing_complete` state channel), so reviving either half first would not have restored a working feature.
- Deferred: DEBT-146 — `is_indexing_complete` graph-state channel is write-only (surfaced by this cascade); left alone, since removing a checkpoint-persisted `TypedDict` field is a contract change out of scope for a cleanup pass.

## 12.13: Symbol-Level Chunk Embeddings — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/455 · pytest 2754 passed, 2 skipped, 1 pre-existing unrelated fail · pyright 0
- Shipped: closed DEBT-140 — hybrid-by-size chunking. Files over `_CHUNK_FILE_MIN_TOKENS` (800) additionally embed one vector per function/method into a new additive `symbol_chunk_embeddings` table, sourced from the parse the indexer already does (zero re-parses); `search_snippets` merges both tables under one `asyncio.gather` with a nearest-first per-file evidence budget; routing meters (CSS) stay on the file table only, proven byte-identical either way. New `POST /api/v1/memory/chunks/backfill` adopts an already-indexed corpus, since no full-reindex path exists anywhere in this codebase. `semantic_delete`, vector GC, and the dimension-recreate path all extended to both tables.
- Key decision: chunk-vector reuse is keyed on a sha256 of the chunk's own text, not `(qualified_name, start_line)` — line numbers shift on any edit above a symbol, so a positional key would re-embed the whole file on an unrelated one-line insert. Corrected the manifest's original cost claim: the file-level vector is still re-embedded every save regardless of reuse; incremental cost is `1 + M` (M = symbols edited) against the pre-chunking `1`, not a flat improvement.
- Deferred: DEBT-147 (cold-path `symbol_definitions` catalog gap), DEBT-148 (dashboard chunk visualization), DEBT-149 (CSS recalibration against chunk distances) — all registered, none blocking this closure.

## 12.7: Coder Tool-Calling Completion (finishes Division 8.18) — 2026-08-03
**Status:** COMPLETE | **Gates:** mypy 0/456 · pytest 2813 passed, 2 skipped · pyright 0
- Shipped: closed DEBT-130 (bounded READ_ONLY tool-grounding pre-pass ahead of `run_coder_node`'s one-shot SEARCH/REPLACE call, gated by a needs-grounding heuristic + an ASK_ALL admission check so it never burns a round-trip it can't use), DEBT-129 (`ToolDispatcher.classify()` seam lets the agentic cell's registry fallback defer a HITL-tier tool via new `pending_tool_call` state channel instead of denying it, resolved by exact name — never re-ranked — on resume), DEBT-106 (dispatched dev-role subagents resolve tools through the same `select_tools`/`resolve_tools` substrate the cell already used, closing the tool-less gap), and DEBT-127 (`agents/roles.py::build_subagent_system_prompt` threads a saved per-role override into both the subagent's tool loop and its final-answer synthesis). Fixed two latent substrate defects found while widening `resolve_tools()`'s consumer count from one to three: `core/tool_registry.py`'s state-bound factories read a `session_id` channel `AIlienantGraphState` never carries (always bound `None`), and `core/tool_rag.py::select_tools`'s PLAN pre-filter tested the deprecated legacy alias, not the canonical `PLAN_ONLY` member new `normalize_session_mode()` now backs both against.
- Key decision: the coder's one-shot path gets a READ_ONLY ceiling only, not tier parity with the cell — it is re-entered by the `error_correction` retry loop, so a mutating call there would violate the idempotency invariant; mutation stays exclusively the cell's surface (DEBT-068's standing ruling).
- Deferred: none new — DEBT-137 (`docs/TECH_DEBT_BACKLOG.md`) re-logged with its blocker resolved but its premise re-evaluated: the grounding pre-pass's schemas live in a separate reasoning call, not the coder's stable system-message HEAD, so the cacheable prefix still doesn't clear the provider floor.

## 12.9: Manifest & Backlog Ledger Accuracy Pass — 2026-08-04
**Status:** COMPLETE | **Gates:** docs-only, no code gate applies
- Shipped: the two literal staleness claims in this item's spec (8.14/8.15 dashboard rows, DEBT-057/058/059/078 schedule) were already corrected by an earlier sweep — verified, no-op. Pruned `TECH_DEBT_BACKLOG.md`'s Open Items Dashboard from ~95 rows to 43 by removing every already-RESOLVED/CLOSED/INVALID entry, keeping only genuinely open ones.
- Key decision: cross-referenced every open row's Target Phase against actual manifest closure rather than trusting the Schedule column, which surfaced two silently-resolved entries the spec never named — DEBT-110 and DEBT-112 were both actually fixed by 11.4 (commit 6ae8eb3) but never closed in the ledger; DEBT-112 had no body entry at all until this pass. DEBT-025's stale "Phase 7.19" pointer corrected to note the phase closed without resolving it (mirrors the DEBT-035 precedent); DEBT-075's dashboard/body schedule-state mismatch (Floating vs. its own Unscheduled tag) fixed.
- Deferred: none.

## 12.14: Pre-13 Critical Debt Closure — 2026-08-04
**Status:** COMPLETE | **Gates:** mypy 0/460 · pytest 2856 passed, 2 skipped, zero footnoted flakes · pyright 0
- Shipped: closed DEBT-152 (agentic-cell orphaned-session sweep wired to both the run-lifecycle done-callback and a WS-disconnect safety net, guarded against a HITL-paused session and a successor task), DEBT-150 (interruptible PTY exec-socket reads + exec creation moved off the shared default executor onto the bounded `ail-docker` pool + lease release on a failed open), DEBT-151 (bounded FIFO admission queue in front of the sandbox pool's same-mount share degrade), and DEBT-153 (autouse `response_cache.clear()` fixture). DEBT-098/081/154 carry a logged defer decision (CLAUDE.md §4), not silence.
- Key decision: DEBT-108's footnoted "test flake" was re-diagnosed as a real production defect — `filelock`'s thread-local context made a cross-thread `FileLock` release silently no-op, leaking the lock on every benchmark run; fixed by collapsing the critical section into one `asyncio.to_thread` dispatch rather than weakening the test's strict cap assertion. Found and fixed the identical pattern in `core/memory/docs_index.py`, and an adjacent non-identity-safe done-callback in `TaskService.register_active_task` that DEBT-152's successor guard exposed.
- Deferred: none new.
