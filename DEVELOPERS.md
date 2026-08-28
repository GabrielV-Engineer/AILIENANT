# AILIENANT — Developer Guide (Internal)

> **Audience:** core contributors working *on* AILIENANT. This is the deep technical reference — architecture, the execution graph, subsystem internals, pseudocode for the load-bearing paths, the security model, and an honest map of what is and isn't built. If you're a *user*, start with [HowToUseIt.md](HowToUseIt.md); for a gentle architectural tour, see [HowItWorks.md](HowItWorks.md).
>
> **Source of truth for status & roadmap:** [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) and [docs/DEV_JOURNAL.md](docs/DEV_JOURNAL.md) (Phase 8.x active log) / [docs/DEV_JOURNAL_ARCHIVE.md](docs/DEV_JOURNAL_ARCHIVE.md) (Phase 0–7.19 history). Where this guide and those disagree, they win. Architectural contracts live in the `docs/PHASE_*_BLUEPRINT.md` files and `docs/SCHEMA_EVOLUTION.MD`.

---

## Contents

- [System shape](#system-shape)
- [The execution graph](#the-execution-graph)
- [The agents](#the-agents)
- [Core subsystems](#core-subsystems)
- [The security model](#the-security-model)
- [Repository layout](#repository-layout)
- [Tech stack](#tech-stack)
- [API surface](#api-surface)
- [Configuration](#configuration)
- [Testing & quality gates](#testing--quality-gates)
- [Design principles](#design-principles)
- [Honest list of what is NOT implemented](#honest-list-of-what-is-not-implemented)
- [Further reading](#further-reading)
- [Debugging runbook](docs/DEBUGGING_RUNBOOK.md)

---

## System shape

Two processes, one contract:

```
┌────────────────────────────────────┐         ┌──────────────────────────────────────┐
│  VS Code Extension (TypeScript)    │         │  ailienant-core (Python, FastAPI)    │
│  ──────────────────────────────    │         │  ──────────────────────────────────  │
│  • Sidebar webview (React)         │         │  • FastAPI app + WebSocket gateway   │
│  • Web Dashboard SPA (ESM)         │  HTTP   │  • LangGraph state machine            │
│  • VFS reader (dirty buffers)      │ ──────► │  • Hybrid LLM router (CSS × TCI)      │
│  • PatchActuator (applyEdit)       │  WS     │  • GraphRAG retrieval                 │
│  • Streaming markdown + tokenizer  │ ◄────── │  • Sandbox tiers + closed-loop exec   │
│  • Telemetry / silent-rejection    │         │  • Token Ledger + FinOps supervisor   │
└────────────────────────────────────┘         └────────────────┬─────────────────────┘
                                                                 │
                                        ┌────────────────────────┼──────────────────────┐
                                 ┌──────▼──────┐         ┌────────▼───────┐     ┌─────────▼────────┐
                                 │  LanceDB    │         │  SQLite (WAL)  │     │  LiteLLM proxy   │
                                 │  vectors    │         │  catalog +     │     │  / direct BYOM   │
                                 │  (HNSW)     │         │  checkpoints   │     │  (local + cloud) │
                                 └─────────────┘         └────────────────┘     └──────────────────┘
```

The extension is intentionally thin: it captures editor state, renders the agent's work, and actuates approved edits. All cognition lives in the Core.

---

## The execution graph

The single entry point is `alienant_app.astream(...)` ([core/task_service.py](ailienant-core/core/task_service.py)), the compiled LangGraph app from [ailienant-core/brain/engine.py](ailienant-core/brain/engine.py), running over a strictly-typed `AIlienantGraphState` ([brain/state.py](ailienant-core/brain/state.py)). Every task — from a one-line edit to a full mission — traverses this one graph; there is no separate topology selected by task size. What varies per-turn is the **Effort Budget** (`effort_level: light | balanced | deep`), which controls verification depth (lint gate, self-heal retry ceiling, whether the plan's own acceptance `checks` run) rather than which nodes execute.

```
START
  → summarize_history
  → session_delta_aggregator
  → [planner_mode_active?]
       yes → ideation_loop  (Socratic clarification)         → END (suspend on HITL)
       no  → researcher_agent  (owns retrieval + routing cascade; emits skeleton + context_metrics)
               → planner_agent  (pure WBS engine; consumes the routing signal)
               → step_dispatch  (fan-out anchor; validate_output loops back here to advance a step)
                 → route_to_coders          (SWARM if cloud, RELAY if local)
                   → coder_agent (×N parallel in cloud)
                     → contract_guard       (assert workspace state before write)
                       → finops_gate        (cost < ceiling?)
                         → supervisor_node   (FinOps hard-kill or proposal)
                           → apply_patch     (PREPARE: diff/risk/verdict, no interrupt)
                             → apply_commit  (GATE: interrupt-first HITL approval; AST syntax gate on the overlay, then the actual write/exec)
                               → validate_output (state-shape check + trajectory persistence — not a code gate)
                               → [retry / advance / heal / verify?]
                                   → coder_agent       (retry the same step)
                                   → drift_gate        (advance: next pending WBS step — the RELAY multi-step loop)
                                   → run_checks        (all steps terminal AND effort_level=deep: execute the plan's own acceptance checks)
                                   → error_correction | agentic_cell | END
```

Every node transition is persisted by a `HybridCheckpointer` over SQLite WAL, so every super-step is **durable, resumable, and branchable** (time-travel). The conditional loop-back edges (`route_after_coder`, `route_after_cell`, and `route_after_validation`'s WBS-advance) are what turn each repair/step iteration into its own checkpoint. A WBS step's status is written as a returned `mission_spec` state delta (never an in-place mutation), so the loop advances reliably across checkpoints.

**Node executor pattern** (planner and coder share this shape):

```python
async def run_<node>_node(state, config) -> dict:
    # 1. assemble context  (workspace overview + GraphRAG + rules + trajectory memory)
    # 2. inject into the prompt with XML/uuid-tag sandboxing of untrusted content
    # 3. call the gateway:  await LLMGateway.ainvoke(model, messages, response_format=...)
    # 4. parse + Pydantic-validate the structured output (retry on ValidationError)
    # 5. return a *state delta* (LangGraph reducers merge parallel fan-out)
```

---

## The agents

Five named agents (Researcher → Planner → Orchestrator → Coder → Analyst) plus a deterministic safety/execution mesh (`drift_compute`/`drift_gate`, `error_correction`, `agentic_cell`, `contract_guard`, `finops_gate`, `supervisor`, `validate_output`). Researcher, Planner and Coder are the fully-wired cognitive core; the Analyst is shipped; the Orchestrator remains emerging (see the [honest list](#honest-list-of-what-is-not-implemented)). In-graph HITL gates (FinOps budget, drift, agentic-cell command) suspend the graph via native LangGraph `interrupt()` and resume on `Command(resume=…)` — no coroutine is pinned awaiting a human.

### Researcher — [agents/researcher.py](ailienant-core/agents/researcher.py)

A **first-class graph node** (`researcher_agent`, spliced before `planner_agent`) that owns the entire information-retrieval domain: a bounded READ_ONLY `ToolDispatcher` grounding loop (`glob`, `grep`, `workspace_structure`, `query_graphrag`, `get_dependents`), GraphRAG deep-context, fast-boot, @-mention bypass, recency, the Context Meter Cascade (CSS → red-alert → mini-judge → routing) and the hardware reroute. It emits the routing signal (`context_metrics`, `css`, `tci`, `provider`, `routing_warning`) plus a dense AST **skeleton map** that the Planner consumes as its structural view of the codebase. This is the SRP split: the Researcher gathers, the Planner reasons.

### Planner — [agents/planner.py](ailienant-core/agents/planner.py)

Turns intent into a schema-valid `MissionSpecification` (outcome, scope, constraints, decisions, WBS steps, acceptance checks). It **never executes code**: zero tool-use, just an LLM call plus JSON parsing.

- Consumes GraphRAG context and a bounded workspace overview ([agents/workspace_context.py](ailienant-core/agents/workspace_context.py)).
- Its tier is floored at `LOCAL_MEDIUM`: the router still owns escalation, but a plan's SHAPE gates the whole turn, so the smallest model is never an option here (the coder keeps the full four-tier range).
- Routes via the CSS × TCI matrix + Mini-Judge veto (see [Hybrid routing](#hybrid-routing)).
- Its system prompt enforces scope discipline (touch only named/necessary files) and polyglot-file safety (SEARCH/REPLACE only on mixed-syntax files).

### Coder — [agents/coder.py](ailienant-core/agents/coder.py)

Takes one WBS step and emits a patch as git-conflict-style SEARCH/REPLACE blocks (never JSON-escaped). The apply gate ([brain/apply_gate.py](ailienant-core/brain/apply_gate.py)) runs an **AST parse** (Tree-sitter, 20+ grammars, derived from the same language map the indexer uses) on the virtual overlay before anything hits disk — the coder's own output is never trusted unchecked. A file that fails to parse never reaches disk; it re-dispatches through the self-heal loop bounded by `CORRECTION_MAX_ATTEMPTS`, and a step that still does not parse past that bound is reported failed, not silently accepted. LSP-level lint (ruff/eslint/mypy/…) exists in [tools/validation/lsp_filter.py](ailienant-core/tools/validation/lsp_filter.py) but is not yet wired into this path — see the [honest list](#honest-list-of-what-is-not-implemented).

- Bounded local retries; on the configured strike count it escalates to a cloud "surgeon."
- `run_command` steps dispatch into the resolved sandbox tier and read a **structured** verdict — see [the closed-loop executor](#closed-loop-execution).
- When the current file + GraphRAG context is thin (new file, empty RAG hit, or a retry after failed validation), a bounded READ_ONLY tool-grounding pre-pass runs first — the same `core/tool_registry.py` → `core/tool_dispatch.py` substrate the agentic cell and dispatched subagents use, tier-filtered to READ_ONLY so mutation stays on the cell's surface. The SEARCH/REPLACE generation call itself never gains tool-calling of its own; grounding observations are folded into its context as ordinary (trimmable) prompt content.

For steps the planner flags as needing iteration, control routes into the **agentic cell** ([brain/agentic_cell.py](ailienant-core/brain/agentic_cell.py)): a bounded ReAct loop over a live, persistent terminal ([core/pty_session.py](ailienant-core/core/pty_session.py) — one long-lived shell owns `cwd`/`env`, async byte-stream with backpressure, Ctrl-C, teardown) exposing exactly three strict-schema tools — `run_terminal` (structured diagnostics, never raw stdout), `read_file_ast` (skeleton, not full file), and `apply_granular_edit` (transactional SEARCH/REPLACE with an optimistic-concurrency guard). A registry-fallback tool outside those three resolves through the same substrate; a HITL-tier fallback call defers (mirroring `run_terminal`'s own HITL defer) rather than being silently denied, and executes on the next super-step once the operator approves.

### Orchestrator

Deterministic driver of the WBS: sequences steps, threads state, and routes each step's tier. *Emerging:* its operations are direct state access today; [División 8.8](docs/PROJECT_MANIFEST.md) formalizes them as audited, callable tools (`get_wbs_status`, `get_token_ledger`, `emit_hitl_request`) so an external gateway can invoke them safely.

### Analyst (Natt) — [agents/analyst.py](ailienant-core/agents/analyst.py)

The read-only conversational tutor in the side panel (the *voice*, not the *hand* — it never edits files). It runs inside the optional `ideation_loop` sub-graph and grounds answers in a **tri-brain** context: the code GraphRAG (central, model-independent), the workspace README (size-aware digest, [core/readme_digest.py](ailienant-core/core/readme_digest.py)), and AILIENANT's own product docs-RAG (reserved LanceDB namespace, [core/memory/docs_index.py](ailienant-core/core/memory/docs_index.py)). A `ContextBudgetManager` packs whole chunks by real tokens with a per-brain soft-cap; the answer model tier is user-selectable and fully decoupled from retrieval.

---

## Core subsystems

### Hybrid routing

CSS quantifies how much of the right context we hold; the Mini-Judge is a cheap semantic-risk veto.

```
CSS = (0.5·semantic_similarity + 0.3·graph_coverage + 0.2·recency) × 100
red alert  ⟺  CSS < 40   → bypass the judge, escalate to CLOUD

Mini-Judge verdict:
  HIGH    → veto to CLOUD, force tci = 100
  MEDIUM  → escalate LOCAL_SMALL → LOCAL_BIG, clamp tci ≥ 75
  NONE    → defer to math:  tci < 30 → SMALL ;  < 75 → BIG ;  ≥ 75 → CLOUD
```

Source: [agents/planner.py](ailienant-core/agents/planner.py), [core/memory/context_auditor.py](ailienant-core/core/memory/context_auditor.py), [brain/routing_engine.py](ailienant-core/brain/routing_engine.py).

### GraphRAG retrieval

`SemanticMemoryManager.search_with_paths` runs one embedding + cosine search → `(score, top_k_files)`. `GraphRAGDynamicExtractor.deep_parse` expands those seeds one hop through the SQLite `dependency_graph` table, reads each through the VFS firewall, parses with Tree-sitter in `asyncio.to_thread`, and emits a `DeepParseResult` (target/parsed files, formatted block, coverage ratio, token count). Depth `k`, file cap, and token ceiling scale per tier (LOCAL_SMALL → k=1 / 10 files / 4K tokens; CLOUD → k=3 / 50 files / 32K tokens).

### Cognitive Fast-Boot

`dump_state_to_markdown` ([core/state_manager.py](ailienant-core/core/state_manager.py)) writes a human-readable checkpoint with an embedded machine-JSON payload to `<workspace>/.ailienant/AGENTS.md` via temp-file + `os.replace`. `load_state_from_markdown` returns `None` if missing or older than `max_age_seconds` (default 3600). On a warm hit, the planner skips the LanceDB embedding call and runs only `deep_parse`. The sibling `dump_plan_to_markdown` writes a separate navigable plan (no embedded JSON) to `<workspace>/.ailienant/plans/<task_id>.md` for the editor preview.

### Storage Home

[core/storage_paths.py](ailienant-core/core/storage_paths.py): global stores (catalog SQLite, MCTS, gateway ledger, the global LanceDB tables) live under `~/.ailienant/`; only the GraphRAG `workspace_embeddings` store is partitioned per project at `~/.ailienant/projects/<project_id>/lancedb/`, bound on `client_workspace_init` (or resolved explicitly via `graphrag_lancedb_path_for(project_id)` by the out-of-process gateway and the dashboard). Legacy CWD-era stores are migrated into the home once at import.

### Memory Janitor

[core/janitor.py](ailienant-core/core/janitor.py): vector GC drops LanceDB rows (filtered by `workspace_hash`) whose source file no longer exists; graph purge deletes obsolete pruned MCTS episodes. Triggered via `POST /api/v1/system/janitor`.

### Dreaming (memory consolidation)

[brain/daemon.py](ailienant-core/brain/daemon.py) — `OvernightDaemon`. **On-demand only: it holds no timer and no loop** (an idle trigger would peg CPU, race a resuming typist, and burn tokens). A pass is fired by `client_dreaming_run` (HUD/command), reads a bounded `build_workspace_overview`, asks `MODEL_MEDIUM` to distill durable facts/patterns/debt into a ≤1024-token note (optionally scoped to a `focus_area`), and upserts it to `.ailienant/dreams/<slug>.md` in semantic memory. It is **read-only** (never edits source), gated by the FinOps session ceiling, runs the network call *outside* `graph_write_lock` (which wraps only the final write), and aborts without writing on a mid-run `stale_check` (OCC). HUD profiles (Medium/Big/Cloud/Hybrid, [DreamingMode.tsx](ailienant-extension/src/workspace/components/DreamingMode.tsx)) bound tasks/files/time per the active BYOM tier. The deeper MCTS patch-exploration loop is future work (see the honest list).

### Memory visualization

The dashboard's Memory panel renders the GraphRAG index four ways. The flagship is a custom three.js **"Neural Nebula"** ([nebula/](ailienant-extension/src/dashboard/panels/memory/nebula/)): an InstancedMesh of glass-crystal node spheres (Fresnel-rim + emissive-core shader, not real transmission — it wouldn't instance), a d3-force-3d one-shot-then-frozen layout, raycast picking, sub-1% breathing, and a search pulse over matched nodes + incident edges; `three` is lazy code-split so it loads only when the 3D tab opens. The 2D [CodeGraphLayer.tsx](ailienant-extension/src/dashboard/panels/memory/CodeGraphLayer.tsx) (ReactFlow) is the WebGL-less / reduced-motion / accessible fallback — now force-directed with a pulse highlight. [VectorMapLayer.tsx](ailienant-extension/src/dashboard/panels/memory/VectorMapLayer.tsx) is a PCA density heatmap, and [EmbeddingBrowser.tsx](ailienant-extension/src/dashboard/panels/memory/EmbeddingBrowser.tsx) a paginated/sortable per-file list with HITL-confirmed purge. Data is plain HTTP — `GET /api/v1/memory/{graph,vectors,embeddings}` and `POST /api/v1/memory/embeddings/purge` from [api/memory_dashboard.py](ailienant-core/api/memory_dashboard.py). No WebSocket — the dashboard is a same-origin REST SPA. (Node types encode only the two the file-level substrate has, `file`/`external-dep`; "centrality" is `nx.degree_centrality`.)

### Tool registry

Tools are role-gated `ToolSchema`s in a RAM-resident LanceDB store ([core/tool_rag.py](ailienant-core/core/tool_rag.py), `ToolRAGStore`): each declares a `ToolPrivilegeTier` and an `allowed_roles` frozenset, enforced at dispatch. All 12 `register_*_tools` families (~53 tool classes) are registered into the catalog at FastAPI startup via `populate_tool_catalog()` ([main.py](ailienant-core/main.py) lifespan); MCP tools are harvested into the same store at session bootstrap. `core/deferred_tool_loader.py` decides per turn whether to inject the whole role/session-visible catalog (when it fits the context budget, zero embedding cost) or retrieve the top `TOOL_RAG_TOP_K=5` by intent — always keeping `tool_search` available as an on-demand escape hatch in the deferred case.

The name→callable bridge — [core/tool_registry.py](ailienant-core/core/tool_registry.py)'s `resolve_tools()` — maps a selected `ToolSchema` to a constructed, dispatch-ready `RegisteredTool`, reusing each tool family's own `build_*_tools(state)` factory where one exists. A small, explicit `_INTENTIONALLY_UNREGISTERED` set documents the handful of schemas deliberately excluded (redundant with a live primitive, or owned by a separate process) — checked by a reachability gate so a new tool class can never go silently unwired again ([División 8.18](docs/PROJECT_MANIFEST.md)).

### Closed-loop execution

The reliability core. `agents/coder.py` only validates the command string (`tools/execution_tools.py::validate_step_command` — fails closed on an empty/placeholder/bare-path command) and stages it in `pending_step_command`; the actual dispatch, permission verdict, HITL approval, and self-heal all live downstream in `brain/apply_gate.py`'s `apply_commit` node (`run_apply_commit_node` → `_commit_command`), which reads the **typed** `SandboxResult.exit_code` — never string-sniffed from stdout:

```python
result = await run_guarded_command(command, session_id=…, session_permission_mode=None)  # gate already decided above
diagnostics = parse_diagnostics(result)          # tools/validation/diagnostics.py — total, never raises
if result.exit_code == 0:
    state["mission_spec"] = _mark_step_status(mission, step_number, "completed")
else:
    # emit a reflexion-mimicking healing delta; route_after_validation carries it to error_correction
    state["healing_required"] = True
    state["last_error_trace"] = format_diagnostics(diagnostics)   # bounded
# correction budget concedes rather than looping forever
```

If no adapter resolved (checked earlier, in `agents/coder.py`), the step is honestly surfaced as deferred (`EXECUTE_TIER_DEFERRED`) rather than falsely "completed."

### State management

`AIlienantGraphState` ([brain/state.py](ailienant-core/brain/state.py)) is a strict `TypedDict` with custom reducers for parallel fan-out keys (`vfs_buffer`, `generated_code`, `current_cost_usd`). Turn-scoped accumulators (`applied_files_log`, `applied_step_ids`, `check_results`, `errors`) are explicitly re-seeded empty each turn — an omitted key keeps its checkpointed value, which for an `operator.add` channel means it accumulates across the whole session.

---

## The security model

AILIENANT assumes an autonomous agent will eventually misbehave and is built to contain it. The pieces:

### Sandbox tiers — [core/sandbox.py](ailienant-core/core/sandbox.py)

A pluggable `SandboxAdapter` ABC with a startup resolver that degrades by safety:

```python
def resolve_default_adapter():
    if docker_reachable():     return DockerSandboxAdapter()      # read-only mount, --network none, non-root, kernel `timeout`
    if wasmtime_available():   return WasmSandboxAdapter()        # WASI pure-compute, 5M-instruction fuel cap, no preopens
    return NativeHITLSandboxAdapter()                             # host spawn, gated by request_human_approval, SANDBOX_DEGRADED_EXEC sentinel
# binds ACTIVE_TIER / ACTIVE_ADAPTER; read via get_active_adapter()
```

Docker is the daemon-pattern default. Rather than one process-lifetime container, `DockerSandboxAdapter` leases from a bounded `_ContainerPool` keyed by `(mount root, session)` — concurrent sessions (and concurrent projects) get their own container instead of contending for one CPU/memory envelope and one `/work` tmpfs, and a session against a different project can never silently fall back onto another project's mount. Pool exhaustion shares a same-mount lease (never a cross-mount one — that would execute against the wrong project) or degrades to `[sandbox_pool_exhausted]`. Every Docker SDK call routes through a timeout-bounded, breaker-guarded dispatcher (`core.sandbox.docker_call`) on a dedicated thread pool, so a hung daemon degrades to `[sandbox_daemon_unavailable]` instead of parking a worker thread from the shared executor every other subsystem depends on. `_DockerPtyBackend` drives interactive sessions and releases its lease exactly once on close. Wasm is fuel-metered with a module-import scope guard. NativeHITL is the degraded fallback — every run requires human sign-off.

A fourth tier, `DevcontainerSandboxAdapter`, targets the user's *trusted* project environment (`devcontainer.json`): instead of shelling Docker it routes `execute()`/`open_session()` over a `HostExecutionBridge` to the IDE host, which owns `devcontainer up`/`exec`. It mirrors NativeHITL's off-process discipline (lazy single-flight provisioning, DLQ-on-timeout, never-crash degrade). It is **not** selected by the safety resolver above — it is reserved for trusted execution and selected separately, leaving the untrusted benchmark oracle on the locked Docker cage. The extension-side lifecycle owner (`DevcontainerProvisioner` in `src/providers/devcontainerProvisioner.ts`) is built and dormant — it probes for the `devcontainer` CLI, drives `devcontainer up`/`exec` as a child process with single-flight provisioning and SIGTERM→SIGKILL timeout degrade, and exposes a status-listener seam. The tier is **routable end-to-end**: the additive WS host-bridge contract (`SCHEMA_EVOLUTION.MD §26`: provision request/status + exec request/stream/exit, `request_id`-correlated, env **names-only**) is consumed by the concrete `WebSocketHostBridge` (`api/devcontainer_bridge.py`, injected into `core.sandbox` from the `main.py` composition root via `set_trusted_bridge` — dependency inversion, `core` imports no transport layer), the `main.py` receive-loop, and the extension host handler (`providers/devcontainerExecHandler.ts`) that drives the provisioner. Trusted `run_command` selection is one chokepoint, `core.sandbox.resolve_execution_adapter(session_id, trusted=True)`, wired at the three live sites (`agents/coder.py`, `core/task_service.py`, `tools/execution_tools.py`). **Selective HITL fallback:** when the devcontainer is unavailable *before* a command runs (no bridge / provisioning failed / no `devcontainer.json`), the adapter delegates to the HITL-gated `NativeHITLSandboxAdapter` (propose → consent → host-native, or DLQ) rather than hard-failing or using the untrusted cage; a *mid-execution* failure degrades in place (idempotency). The *AILIENANT: Scaffold devcontainer* command writes a starter `.devcontainer/devcontainer.json` to restore isolated execution. The `devcontainer` CLI is a **host prerequisite** (PATH or the Dev Containers extension), not bundled in the `.vsix`; `@devcontainers/cli` is a dev-only dependency and the driver degrades with an actionable remediation when neither source is present.

### Fail-closed privilege classification — [core/permissions.py](ailienant-core/core/permissions.py), [core/mcp_registry.py](ailienant-core/core/mcp_registry.py)

`classify_tool_privilege()` decides a tool's `ToolPrivilegeTier` with **catalog > verb-heuristic > DANGEROUS** precedence:

```
1. curated catalog override?     → use it          (core/mcp_registry.py SSoT)
2. else tokenize the verb         (camelCase + snake_case split)
   match against _VERB_SETS       → READ_ONLY | WRITE | EXECUTE
3. else                           → DANGEROUS       (unknown ⇒ hostile until allow-listed)
```

The curated registry is the single source of truth for regulated MCP servers: it carries install metadata (launcher, args, secret *names* only — never values) and authoritative per-tool tier overrides (e.g. a database `query` reads as DANGEROUS to the heuristic but is genuinely READ_ONLY). Launch commands are constrained to `ALLOWED_MCP_COMMANDS` ([core/mcp_constants.py](ailienant-core/core/mcp_constants.py)).

### Three-axis permission engine

`evaluate_action()` composes three orthogonal axes into a single `PermissionDecision` (ALLOW / HITL / DENY) via a pure, O(1), cached function — no I/O, no LLM:

```
SessionPermissionMode  (PLAN blocks non-READ · DEFAULT asks on WRITE/EXEC/DANGEROUS · AUTO runs)
        ×  ToolPrivilegeTier   (READ_ONLY | WRITE | EXECUTE | DANGEROUS)
        ×  AgentIdentity       (per-agent/role policy, shared/rbac.py)
```

`rbwe_guard()` enforces **read-before-write** by consulting `state["read_files_state"]`: a WRITE to a never-read path is denied.

### Concurrency, audit, isolation

- **OCC** — the coder anchors a `base_hash` of the original content; the host-side actuator rejects the patch if the buffer changed underneath it (stale-guard), prompting you instead of clobbering.
- **Audit ledger** — [core/audit.py](ailienant-core/core/audit.py): an append-only, blake2b-chained `hitl_audit_log`. `verify_chain()` re-walks and recomputes every hash; tampering breaks the chain. Secrets are scrubbed before write.
- **Multi-tenant** — every retrieval/vector/GC predicate carries `workspace_hash = sha256(workspace_root)`; the VFS firewall ([core/vfs_middleware.py](ailienant-core/core/vfs_middleware.py)) enforces ignore rules, binary detection, and a 500 KB anti-OOM ceiling.

---

## Repository layout

```
Proyect_Ailienant/
├── assets/                      # Public brand assets (logo, icon) used by the READMEs
├── Dockerfile · docker-compose.yml · .dockerignore # App-runtime image (backend + embedded
│                               #   LanceDB) — `docker compose up` from repo root. Distinct from
│                               #   ailienant-core/Dockerfile (the sandbox/command-exec image).
├── .github/
│   ├── workflows/               #   docker-publish.yml (sandbox image → GHCR), backend-gate.yml
│   │                            #     (ruff/mypy/pyright/pytest+cov), frontend-gate.yml (compile/lint/test
│   │                            #     + nightly Playwright e2e)
│   ├── ISSUE_TEMPLATE/          #   bug_report.md · feature_request.md
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml           #   pip (ailienant-core) · npm (ailienant-extension, root) · github-actions
├── scripts/
│   └── pre_commit_backend_gate.py # venv-resolving entry point for the ruff/mypy pre-commit hooks
├── .pre-commit-config.yaml      # ruff + mypy-on-changed-files (ailienant-core) + eslint (ailienant-extension)
├── package.json · package-lock.json # root-level, private:true — pins @fission-ai/openspec only,
│                               #   decoupled from ailienant-extension's own package.json/vsce packaging
├── openspec/                    # OpenSpec CLI narrow verification layer (structural/drift checks only —
│                               #   NOT a migration of docs/PROJECT_MANIFEST.md; new-phases-only, Phase 13+;
│                               #   see CLAUDE.md §15). config.yaml (project context) · changes/ (per-change
│                               #   proposal/spec-delta/design/tasks folders) · specs/ (archived baseline)
├── SECURITY.md · CODEOWNERS     # Vulnerability disclosure path + review routing
├── ailienant-core/              # Python orchestration engine
│   ├── pytest.ini · mypy.ini    #   gate config: unit/integration/e2e markers · typing scope
│   ├── ruff.toml · conftest.py  #   lint rules · shared autouse fixtures (cache/singleton resets)
│   ├── main.py                  #   FastAPI app + WebSocket gateway + lifespan (sandbox resolve)
│   ├── agents/                  #   Graph nodes: planner, coder, analyst, logic, researcher,
│   │                            #     orchestrator, contract_guard, error_correction, inline_edit,
│   │                            #     workspace_context, analyst_context, recency
│   ├── brain/                   #   State machine + routing + checkpointing
│   │   ├── engine.py            #     graph assembly + reflexion/self-heal + agentic-cell wiring
│   │   ├── checks_gate.py       #     run_checks node: executes the plan's own acceptance checks (deep effort only)
│   │   ├── guardrails.py        #     output state-shape check + WBS-advance/run_checks/END routing
│   │   ├── state.py             #     AIlienantGraphState, MissionSpecification, reducers
│   │   ├── subagent_contracts.py #    dispatch schema: SubagentTask / DispatchPlan / result envelopes
│   │   ├── dispatch.py          #     build_dispatch_sends fan-out + wave-split routers
│   │   ├── dispatch_ledger.py   #     budget admission: reserve/commit/refund over current_cost_usd
│   │   ├── dispatch_emitter.py  #     optional planner/researcher DispatchPlan emission (hook + synthetic)
│   │   ├── nodes/               #     subagent_worker + dispatch_synthesize dispatch nodes
│   │   ├── routing_engine.py    #     model keep-alive policy (the CSS × TCI matrix itself lives in core/memory/context_auditor.py)
│   │   ├── context_pipeline.py  #     5-layer context assembler (ContextChunk, ContextPipeline)
│   │   ├── agent_context.py     #     budget-guard over ContextPipeline (build_agent_context)
│   │   ├── agentic_cell.py      #     bounded ReAct cell (re-exports run_tournament as select_candidate_via_mcts)
│   │   ├── subagent_tournament.py #   transactional UCB1 candidate tournament + run_tournament_from_dispatch adapter
│   │   ├── coder_companion.py    #     fire-and-forget structured post-turn explanation (best-effort WS side channel)
│   │   ├── iteration_governor.py #    multi-axis circuit breaker
│   │   ├── retry_policy.py      #     centralized retry/correction budgets + Effort Budget (light/balanced/deep) ceiling resolution
│   │   ├── apply_gate.py        #     incremental per-step approval: apply_patch (PREPARE) + apply_commit (interrupt-first GATE)
│   │   └── mcts/ · episodic/    #     tree + UCB1 + audit checkpointer
│   ├── core/                    #   Infrastructure
│   │   ├── sandbox.py           #     SandboxAdapter ABC + Docker/Wasm/NativeHITL + resolver
│   │   ├── pty_session.py       #     persistent interactive shell sessions (PTY)
│   │   ├── command_boundary.py  #     shared sentinel-marker command-boundary framer (PTY + devcontainer bridge sessions)
│   │   ├── workspace_sync.py    #     bidirectional VFS ↔ sandbox sync
│   │   ├── permissions.py       #     3-axis matrix + RBWE + classify_tool_privilege
│   │   ├── skill_resolver.py    #     dual-mode skill resolver (cosine auto-match + explicit chip) + sandboxed directive block builder
│   │   ├── mcp_registry.py      #     curated regulated-server SSoT (install meta + tiers + source_url)
│   │   ├── mcp_config.py        #     .ailienant/config.json export/import projection
│   │   ├── mcp_constants.py     #     ALLOWED_MCP_COMMANDS allowlist
│   │   ├── supervisor.py        #     deterministic FinOps supervisor (hard-kill / soft gate)
│   │   ├── audit.py             #     append-only blake2b HITL audit ledger
│   │   ├── vfs_middleware.py    #     VFS proxy firewall (ignore + binary + anti-OOM)
│   │   ├── state_manager.py     #     AGENTS.md fast-boot serializer + navigable plans/ export
│   │   ├── storage_paths.py     #     app-home resolver; per-project GraphRAG partition + legacy CWD migration
│   │   ├── project_instructions.py # freeform AILIENANT.md project-instructions reader (token-capped)
│   │   ├── janitor.py           #     orphan-vector GC + MCTS purge + telemetry retention purge (4 append-only tables)
│   │   ├── token_ledger.py      #     LOCAL/CLOUD token accounting
│   │   ├── graph_weight.py      #     pre-execution context-OOM predictor (state tokens vs candidate window)
│   │   ├── observability.py     #     env-gated LangSmith tracing bootstrap (no new sink)
│   │   ├── telemetry.py         #     append-only SQLite audit trail (routing/OOM/latency/tool_invocations); dashboard-read + janitor-pruned
│   │   ├── telemetry_log.py     #     tail-able rotating file sink (.ailienant_telemetry.log; WS/NODE/INDEX/CONTEXT/GENERATION/NETWORK/ERROR categories)
│   │   ├── url_guard.py         #     outbound-fetch destination validation (scheme allowlist; loopback/RFC1918/link-local/metadata refused) + log-safe URL redaction
│   │   ├── redaction.py         #     shared ReDoS-safe secret masker (mask_secrets; used by telemetry + exec_log)
│   │   ├── exec_log.py          #     bounded in-memory per-exec command-output ring (non-persistent, source-tagged, seq-cursor); sole emitter of the Glass-Box Timeline execution-detail channel
│   │   ├── activity_context.py  #     turn-scoped ActivitySink Protocol + ContextVar (Glass-Box Timeline execution-detail correlation, no tool-signature changes)
│   │   ├── deferred_tool_loader.py # eager-vs-deferred tool injection over ToolRAGStore (~10%-budget gate)
│   │   ├── tool_dispatch.py     #     runtime tool-dispatch loop (gated, self-correcting; live on Analyst/Researcher, the agentic cell's registry fallback + HITL defer, the coder's grounding pre-pass, and dispatched dev-role subagents)
│   │   ├── tool_registry.py     #     ToolSchema name -> constructed RegisteredTool bridge (resolve_tools)
│   │   ├── memory/              #     semantic, trajectory, graphrag_extractor, context_auditor,
│   │   │                        #     docs_index (product-docs RAG — reserved LanceDB namespace)
│   │   ├── readme_digest.py     #     workspace README brain: verbatim/digest/head-slice + debounced rebuild
│   │   ├── db.py                #     SQLite catalog (dependency_graph, ppr_scores, indexed_files)
│   │   ├── blast_radius.py      #     pre-apply transitive-dependents mapper (resolved reverse-adjacency BFS)
│   │   ├── memory_snapshot.py   #     portable dependency-graph export/import (.ailienant/memory.db.zst)
│   │   ├── dead_code.py         #     file-level zero-resolved-in-degree, non-entrypoint orphan scan
│   │   ├── symbol_refs.py       #     Tier-2 lazy "who calls this symbol" resolver (FTS5 narrow → AST-confirm; no stored call edges)
│   │   ├── boundary_graph.py    #     cross-boundary WS/MCP seam edges (separate namespaced table; "what handles X"; never pollutes code-dep traversal)
│   │   ├── call_trace_probe.py  #     offline sys.monitoring runtime call-trace harness (reconciles + persists observed_call_edges vs find_symbol_callers; not a runtime import)
│   │   ├── benchmark_service.py #     host-side run_benchmark execution + report store (LFI-hardened, single-flight)
│   │   ├── benchmark/           #     shippable in-process precision/ablation harness (importable without tests/)
│   │   │                        #       arms/runner/hygiene/metrics/problems + codegen (Pass@1) + executors +
│   │   │                        #       oracle (Resolve@k) + strategies + routing_study + report; ships its own
│   │   │                        #       datasets/ (HumanEval/MultiPL-E) and corpus/v1/ (multi-file snapshot);
│   │   │                        #       session_corpus.py (synthetic long-session generator) +
│   │   │                        #       context_telemetry_report.py (CONTEXT-telemetry gate aggregator + CLI)
│   │   └── config/              #     BYOM schema + embedding/model resolvers + profiles
│   │       ├── mcp_secrets.py   #       backend-masked MCP credential store (0600) + connect-time env injection
│   │       ├── model_pricing.py #       best-effort per-model $/token from litellm.model_cost (local=free, unknown=omitted)
│   │       └── host_discovery.py #      ephemeral ~/.ailienant/run.json (port+token+pid, 0600) + async liveness probe
│   ├── api/                     #   WS manager + REST routers (memory, byom, hardware, audit,
│   │                            #     projects [active-project registry], mcp_servers, skills,
│   │                            #     sessions, agent_roles, system_settings);
│   │                            #     devcontainer_bridge.py (WebSocketHostBridge — trusted-tier transport)
│   ├── tools/                   #   llm_gateway, validation pipeline (AST+LSP), MCP adapter
│   │                            #     (multi-session registry + dispatch gate), perception/
│   │                            #     mutation/execution/control/meta/researcher/analyst tool bundles
│   │                            #     (meta_tools.py = tool_search discovery;
│   │                            #      researcher_tools.py = Wave-1 Researcher arsenal;
│   │                            #      analyst_tools.py  = Wave-2 Analyst arsenal (6 READ_ONLY tools);
│   │                            #      orchestrator_tools.py = Wave-3 Orchestrator introspection (2 READ_ONLY tools);
│   │                            #      planner_tools.py = Wave-3b Planner pre-commit verification (2 READ_ONLY tools);
│   │                            #      coder_tools.py = Wave-4 role-specific coder arsenal (10 net-new + ASTValidateTool);
│   │                            #      gateway_tools.py = Wave-5 gateway/benchmark tools (6 net-new: run_benchmark, get_benchmark_report, list_capabilities, skill_invoke, task_list, task_stop);
│   │                            #      universal_tools.py = Wave-6 universal arsenal (todo_write, READ_ONLY, all 12 roles);
│   │                            #      quarantine.py = shared Cognitive-Quarantine boundary wrapper),
│   │                            #     validation/diagnostics.py (structured verdict parser)
│   ├── gateway/                 #   External Capability Gateway: stdio MCP server exposing AILIENANT
│   │                            #     verbs to external agents (catalog + schemas + call-tool routing seam)
│   │   ├── governance.py        #     symmetric permission gate (evaluate_action reuse) + caller_id +
│   │   │                        #       conservative posture (no self-escalation, no silent AUTO)
│   │   ├── handlers.py          #     capability handlers: in-process READ_ONLY memory/graph verbs +
│   │   │                        #       loopback run_task/run_benchmark/check_task_status/get_report over the live host
│   │   └── ledger.py            #     durable per-caller token-bucket + budget DoS guard (filelock, fail-closed)
│   ├── transport/               #   outbound WS stream (throttler, token batcher, narration gate)
│   ├── shared/                  #   config, RBAC, contracts, hardware probe, persona, log filters
│   ├── validators/              #   environment.py: interpreter + typing-config probe (its former syntax/style
│   │                            #     gates module had no production caller and was retired — the live syntax
│   │                            #     gate is tools/validation/ast_filter.py, wired at brain/apply_gate.py)
│   ├── scripts/                 #   standalone, opt-in operator scripts — never pytest-collected
│   │   └── hardware_stress_sim.py # env-gated (AILIENANT_ENABLE_HW_STRESS=1) REAL RAM/VRAM pressure;
│   │                            #     complements tests/chaos/test_hardware_stress_sim.py's synthetic injection
│   └── tests/                   #   pytest suite + per-phase checkpoint gates + chaos crucible
│       ├── e2e/                 #     real HTTP/WS end-to-end (SSoT prompt→graph→WS→applied patch);
│       │                        #       seed_dashboard_fixture.py hermetically seeds the Playwright dashboard suite
│       └── benchmark/           #     hermetic gates for the core/benchmark harness (harness itself lives in core/)
│           ├── test_ablation_verdicts.py  #  5-arm comparable verdicts, provider seam, drain, normalize
│           ├── test_codegen_pass1.py      #  plain-codegen Pass@1 over the frozen dataset subset
│           ├── test_harness_scaffold.py   #  four-arm smoke over the scaffold problem
│           ├── test_oracle_resolve_k.py   #  Resolve@k on golden/wrong patches + patch extraction
│           ├── test_routing_study.py      #  TCI bucketing, H2 savings/retention, anchored pairing
│           ├── test_report.py             #  Wilson, H1 0/0 guard, schema validity, full-matrix sweep
│           ├── test_reproducibility.py    #  DoD-check: pinned-SHA surfaced + byte-deterministic report
│           ├── test_retention.py          #  artifact count cap + LRU-by-mtime eviction, fail-safe config
│           └── report.schema.json #       committed Draft-07 public contract for report.json (read by test_report)
├── ailienant-extension/         # VS Code extension (TypeScript + React)
│   ├── src/
│   │   ├── extension.ts         #     activation entry
│   │   ├── ide_sync.ts          #     context capture (debounced, .ailienantignore gate)
│   │   ├── workspace_provisioning.ts # first-run .ailienant/ skeleton + starter AILIENANT.md + .gitignore block
│   │   ├── webview/             #     React sidebar (chat, ReasoningStream + ∞ glyph, diffs, HUD, checklist)
│   │   ├── dashboard/           #     Web Dashboard SPA (grouped/collapsible nav shell + panels)
│   │   │   ├── panels/          #       Hardware/BYOM/Rules/Staging/Audit/Overview/Memory/…
│   │   │   │   └── memory/      #         GraphRAG viz: CodeGraphLayer (2D), VectorMapLayer, EmbeddingBrowser
│   │   │   │       └── nebula/  #           custom three.js "Neural Nebula" 3D engine (lazy-split)
│   │   │   ├── ui/              #       design-system primitives (Card, StatTile, Button, Badge, Skeleton, EmptyState, ShortcutsOverlay, ConfirmModal, ProjectSelector, ActiveProjectBadge)
│   │   │   │   └── charts/      #         dependency-free SVG chart primitives (RadialGauge, Sparkline, Donut)
│   │   │   ├── hooks/           #       usePollingWhileVisible · useSidebarCollapsed · useKeyboardShortcuts · useActiveProject · useRingBuffer
│   │   │   └── format.ts        #       shared number/currency/size/relative-time formatters
│   │   ├── core/                #     IntentRouter, PatchActuator, tokenizer, inline-edit manager
│   │   ├── workspace/           #    Zustand stores, streaming markdown parser
│   │   │   ├── chatStore.ts     #      memory-only Zustand store — 22 live chat-runtime fields (messages, streaming, toasts, …)
│   │   │   ├── workspaceStore.ts #     persisted Zustand store — UI slice (mode, preset, inflightTurn, drafts, …)
│   │   │   ├── types.ts         #      shared message type exports (ConversationMessage, SystemMessage, Message, NattMessage, …)
│   │   │   ├── Workspace.tsx    #      layout host — selectors + hook calls; <800 lines
│   │   │   ├── hooks/
│   │   │   │   ├── useWSMessageHandler.ts #  no-arg WS dispatch controller (45-branch switch, rAF buffers, watchdog)
│   │   │   │   └── useSessionPersistence.ts # PERSIST_TRANSCRIPT (full trace, reasoning included) + body-stripped budget-trimmed inflight snapshot + mount-rehydrate
│   │   │   ├── components/
│   │   │   │   └── ToastStack.tsx #    presentational toast list (JSX, role="alert" per item)
│   │   │   └── utils/
│   │   │       └── messageDispatchHelpers.ts # pure helpers + dispatch consts (mkId, mergeById, attachOrUpdate*, …)
│   │   ├── sidebar/             #    sidebar webview
│   │   ├── providers/ · api/    #     chat provider, WS client, path index, HITL notifier,
│   │   │                        #     devcontainerProvisioner.ts (vscode-free lifecycle driver — probe/up/exec/degrade),
│   │   │                        #     devcontainerFactory.ts (vscode wiring + lazy singleton),
│   │   │                        #     devcontainerExecHandler.ts (host-side bridge handler — provision/exec, streams back),
│   │   │                        #     devcontainerSessionHandler.ts (§43 interactive-session bridge — stateful, owns live child processes),
│   │   │                        #     devcontainerScaffold.ts (idempotent .devcontainer/devcontainer.json starter),
│   │   │                        #     docsCatalog.ts (vscode-free Help-documents resolver over dist/docs/)
│   │   └── test/                #     vscode-test mocha suite (webview components + dispatch logic)
│   ├── e2e/                     #   Playwright suite for the browser-reachable dashboard SPA (Phase 11.9);
│   │                            #     run-backend.mjs boots a hermetically-seeded ailienant-core, fixtures.ts
│   │                            #     reads the seeded ids, playwright.config.ts owns the webServer/browser wiring
│   ├── media/                   #   source logos (logo.svg, icon-color.svg, icon.svg)
│   ├── dist/docs/               #   build-copied user guides (README · HowToUseIt · HowItWorks) shipped in the VSIX
│   ├── playwright.config.ts     #   Chromium-only smoke config for e2e/ (separate from the tsc/eslint compile gate)
│   └── esbuild.js               #   3 build contexts (extension CJS · webview IIFE · dashboard ESM) + user-guide copy
├── docs/                        # Manifest, blueprints, dev journal, schema, system prompts, tech debt,
│                                #   DEBUGGING_RUNBOOK.md (install-triage map: exec-log ring, Glass-Box
│                                #   Timeline, audit chain, telemetry tables)
├── README.md  (+ 6 translations)# Public landing page
├── HowToUseIt.md · HowItWorks.md# User & architecture guides
├── DEVELOPERS.md                # This document
├── CONTRIBUTING.md · CLA.md     # Contribution guide + CLA
├── LICENSE · LICENSING.md       # AGPL-3.0 + dual-license explainer
└── CLAUDE.md · AGENTS.md        # Coding standards, architectural guardrails, and build protocols
```

> Keep this tree accurate. Per [CLAUDE.md](CLAUDE.md) §14.3, any new file or structural directory must be reflected here.

---

## Tech stack

**Backend (`ailienant-core/`)** — Python ≥ 3.10 (tested on 3.13)

| Layer | Library |
| --- | --- |
| Orchestration | `langgraph`, `langchain-core`, `langsmith` |
| LLM proxy / direct | `litellm` (OpenAI, Anthropic, Google, DeepSeek, Mistral, Ollama, vLLM, llama.cpp) |
| Vector store | `lancedb` + `pyarrow` (HNSW, cosine, IVF) |
| Catalog / checkpoints | `aiosqlite` over SQLite WAL |
| AST | `tree-sitter` (20+ grammars) |
| API | `fastapi`, `uvicorn`, `httpx` |
| Validation | `pydantic`, `pydantic-settings` |
| Tokenization / graph | `tiktoken`, `networkx` |
| Tooling | `ruff`, `mypy`, `pytest`, `pytest-anyio` |

**Extension (`ailienant-extension/`)** — TypeScript 5.9 (strict), React 18.3, esbuild, ESLint 9.

---

## API surface

The Core exposes a REST + WebSocket surface (see [api/](ailienant-core/api/)). Highlights:

| Route | Purpose |
| --- | --- |
| `GET /` | Health probe (extension uses it for auto-start) |
| `POST /api/v1/task/submit` | Submit a task → `task_id` |
| `WS /api/v1/ws/{client_id}` | Streaming events (tokens, thinking, graph mutations, tool chips, telemetry) |
| `GET/PUT /api/v1/byom/config` · `POST /api/v1/byom/test` | BYOM config + endpoint probing |
| `GET /api/v1/hardware/profile` · `GET/POST /api/v1/hardware/mode` | Hardware snapshot + execution-mode preference |
| `GET /api/v1/runtime/status` · `POST /api/v1/runtime/{start-docker,pull-image}` | Sandbox tier + Docker lifecycle |
| `GET/POST /api/v1/mcp/servers` · `POST /api/v1/mcp/test` · `GET …/registry` · `POST …/registry/install` · `…/config/{export,import}` | MCP server CRUD + curated browse/one-click install + portable config |
| `GET /api/v1/audit/{log,stats,verify}` | HITL audit ledger + chain verification |
| `GET /api/v1/sessions/{thread_id}/checkpoints` | Time-travel checkpoint chain |
| `GET /api/v1/memory/{sections,graph,vectors,embeddings}` | GraphRAG browse surfaces (embeddings: paginated/sortable) |
| `POST /api/v1/memory/embeddings/purge` | HITL-confirmed per-file vector eviction |
| `POST /api/v1/system/janitor` | Memory GC |

---

## Configuration

All env vars are read in [shared/config.py](ailienant-core/shared/config.py). The common ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LITELLM_PROXY_BASE_URL` | `http://localhost:4000` | LiteLLM proxy endpoint |
| `AILIENANT_MODEL_SMALL/_MEDIUM/_BIG` | tier-aliased | Per-tier model selection |
| `AILIENANT_MODEL_EMBEDDING` | ada-002 alias | Vector embedder |
| `AILIENANT_MINI_JUDGE_MODEL` | small/cheap | Mini-Judge classifier |
| `AILIENANT_LANCEDB_PATH` / `AILIENANT_CATALOG_DB` | local paths | Stores |
| `AILIENANT_MAX_BUDGET_USD` | per task | FinOps hard ceiling |
| `AILIENANT_PLANNER_DEBUG` | `1` | Synthetic-SDD stub (no LLM) for tests |
| Cloud keys | unset | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY` |
| `AILIENANT_ENABLE_PHOENIX_TRACING` / `PHOENIX_COLLECTOR_ENDPOINT` | unset / `http://localhost:6006` | Opt-in span tracing (read inline in `core/observability.py`, mirroring the LangSmith gate — not `shared/config.py`); see Testing & quality gates below |

---

## Testing & quality gates

Per [CLAUDE.md](CLAUDE.md), every change must keep the gates green (Exit Code 0):

```powershell
cd ailienant-core
.\venv\Scripts\pytest.exe          # full backend suite
.\venv\Scripts\mypy.exe .          # strict typing (the enforced gate)
.\venv\Scripts\ruff.exe check .    # lint
npx pyright                        # catch Pylance/Pyright UI errors

cd ..\ailienant-extension
npm run compile                    # tsc + esbuild, 0 errors
npm run lint                       # ESLint, 0 errors
```

The suite is large (latest gate: **2,858 passing / 2 skipped**, 91% line coverage, `mypy .` clean — see the most recent [DEV_JOURNAL.md](docs/DEV_JOURNAL.md) entry for the current numbers). Coverage is observability only, no hard minimum-% gate — and it measures lines executed, not integration depth: the suite mocks the LLM/vector-store boundary uniformly, so it does not mean the agent/LLM interaction surface is tested against a real model. Each phase ships a sibling **checkpoint-gate** test file (`test_phase*_checkpoint_gate.py`) that re-certifies that phase's contract.

**Structured triage tooling:** every `pytest` run — local or CI — emits `ailienant-core/test-results.xml` (pytest's built-in `--junitxml`, wired via `pytest.ini`'s `addopts`, no plugin dependency), so a failure can be read from that file directly instead of re-parsing scrollback or re-running the suite. Coverage is not on by default locally (instrumenting all ~2,900 tests on every run would slow the inner loop); run it on demand with `pytest --cov=. --cov-report=term-missing` (same invocation CI uses) when you need to see what a change left untested before touching that code. For CPU/latency investigations, `py-spy` is available (`requirements-dev.txt`, dev-only): `py-spy record -- python -m pytest tests/some_slow_test.py` profiles your own child process with no elevation needed; attaching to an already-running foreign PID does need admin rights on Windows.

**Span tracing (opt-in, self-hosted):** `docker compose --profile tracing up -d` starts a local Arize Phoenix instance on `:6006`; the backend picks it up when both `AILIENANT_ENABLE_PHOENIX_TRACING=1` and a reachable `PHOENIX_COLLECTOR_ENDPOINT` are set (off by default — a bare `docker compose up` never starts Phoenix or touches this path). Instruments both LangGraph nodes and every raw `litellm.acompletion` call, giving a real span waterfall per turn instead of the flat `telemetry_log.py` event stream. **Prompts and completions are written into Phoenix's local store when this is on** — local-only and opt-in, but outside `SecretsScrubberFilter`'s reach, unlike every other sink in this project.

**Zero-degradation rule:** your change must not introduce a single new type error or lint warning. **Boy-Scout rule:** if a file you touch already has errors, fix them while you're there. The two advisory ESLint rules added for code-quality metrics (`complexity`, `max-lines-per-function` — see [CLAUDE.md](CLAUDE.md) §16) are exempt from this policy; every other rule stays under it.

**Code-quality metrics (advisory, non-blocking):** `.github/workflows/code-metrics-gate.yml` runs `radon cc`/`radon mi` over `ailienant-core` and `npm run metrics:duplication` (`jscpd`, config in `.jscpd.json`) over both zones, with `continue-on-error: true` — same pattern as the OpenSpec gate. Locally: `cd ailienant-core && .\venv\Scripts\radon.exe cc . -a -s` / `.\venv\Scripts\radon.exe mi . -s` (both exclude `venv/`/`tests/`), and `npm run metrics:duplication` from the repo root. See [CLAUDE.md](CLAUDE.md) §16 and `docs/TECH_DEBT_BACKLOG.md`'s Debt Ratio section for how these roll up.

**Zero-flake policy:** a test that needs a "pre-existing unrelated flake" footnote at phase-closure is a same-sub-phase fix from now on, never a footnote. This formalizes the precedent set by DEBT-108 (`tests/benchmark/test_retention.py`, a genuine cross-thread `FileLock` defect that was waved through as a footnoted flake across at least 6 phase-closure gates before being fixed for real in 12.14) and DEBT-153 (`response_cache` cross-test contamination, closed the same sub-phase). If a test is flaking, stop and fix the root cause or the assertion's contract before moving on — do not re-verify it green in isolation and footnote it as unrelated.

### CI

`.github/workflows/backend-gate.yml` and `.github/workflows/frontend-gate.yml` run the same gates above automatically on every push/PR touching their respective directories — see [DEV_JOURNAL.md](docs/DEV_JOURNAL.md)'s 12.15 entry for what each covers. A `backend-coverage` artifact (observability only, no hard threshold yet) and a `backend-test-results` artifact (the `test-results.xml` JUnit file) both publish on every backend-gate run. The Playwright e2e suite (`ailienant-extension/e2e/`) runs on a nightly schedule, not on every push, since it boots a real backend subprocess and is comparatively slow.

**One manual step CI cannot self-configure:** branch protection is a GitHub repo setting, not a committable file. To actually block merges on a red gate, go to **Settings → Rules → Rulesets**, create (or edit) a ruleset targeting `main`, enable "Require status checks to pass," and add both `backend-gate` and `frontend-gate` (the job names). This only takes effect after each workflow has run at least once on the repo (GitHub only lists status checks it has seen before) — and note both jobs must have distinct names for the checks picker to disambiguate them; a shared generic job name like `gate` shows up ambiguously.

---

## Honest list of what is NOT implemented

Documentation should never oversell. As of this writing:

- **MCP dispatch wiring is shipped at the adapter layer.** Auto-connect on server startup (idempotent multi-session registry, teardown wired into FastAPI lifespan) and the `evaluate_action` dispatch guard in `McpToolAdapter._arun` (DENY/HITL/ALLOW per the privilege matrix; READ_ONLY friction-free; catalog overrides bind live at harvest) are shipped. What remains: trust-once session-scoped valve (DEBT-029, floating) and FE HITL-card binding for `MCP_TOOL_CALL` (floating). The runtime LLM tool-dispatch loop that invokes registered tools from within the graph is live on the Analyst, the Researcher, and — since Division 8.18 — the Coder's iterative (`brain/agentic_cell.py`) path, via an additive fallback that resolves any tool name outside its 3 hardcoded primitives through `core/tool_registry.py`. The Coder's one-shot `run_coder_node` path (steps the planner does not flag as needing iteration) runs a bounded READ_ONLY tool-grounding pre-pass ahead of its SEARCH/REPLACE call — see the tool catalog entry below for why it stops at a read-only ceiling. Planner and Orchestrator remain excluded by DEBT-068's own architectural ruling: the Planner is PLAN-only/READ_ONLY (a dispatch loop adds no HITL value), and the Orchestrator is a deterministic O(1) node with no LLM to drive one.
- **A Plan-mode turn now stops three times, and the third stop is the brief.** After the Socratic rounds, `run_synthesis_node` distils the dialogue into the brief that *replaces* `user_input` and becomes the planner's requirement statement — and shows it to the operator first (`BriefReviewCard`, in chat) to accept, edit in place, or send back with a correction. That stage was previously the only one in the pipeline with no check at all: the coder has `validate_output` plus acceptance checks, the planner its Actor-Critic loop, the grill a human on every round, while the critic validates the resulting *plan* against a schema and never against the dialogue. The node runs as two graph super-steps on a `synthesis_node` self-loop for the same reason `pending_grill_batch` exists — LangGraph replays a node from the top on resume, so drafting and interrupting in one invocation would re-run the `MODEL_BIG` distillation on every review round. If you add another interrupting node, copy that shape; `tests/test_ideation.py` pins it by asserting the distillation runs exactly once per accept cycle.
- **Adding a graph node: register it through a wrapper, never bare.** LangGraph decides what to inject by inspecting the OUTERMOST callable handed to `add_node`, so a wrapper that hides the node behind `*args, **kwargs` silently costs it every runtime dependency on `config.configurable` — narration, the reasoning sink, the activity channel, the cell dispatcher — with no error and no failing test. `brain/engine.py::_instrument_node` and `brain/ideation.py::_guarded` declare `config: Optional[RunnableConfig] = None` explicitly for exactly that reason and forward it via `brain/state.py::accepts_config`; they deliberately avoid `functools.wraps`, which would set `__wrapped__` and hand the runtime the inner signature again. If you register a node bare, annotate its own `config` as `Optional[RunnableConfig]` and nothing else — the runtime matches that annotation against a literal tuple, and `RunnableConfig | None` is not in it. `tests/test_graph_config_injection_gate.py` certifies this through a real compiled graph and carries its own negative control.
- **Wasm sandbox tier is built but not the production default.** The resolver prefers Docker; Wasm is a pure-compute fallback. gVisor-class isolation is not present.
- **Full MCTS rollout is deferred.** The tree, UCB1 selection, and pruning exist; the only *live* MCTS edge is the contained candidate-selection inside the agentic cell. The offline rollout loop is future work.
- **Dreaming is on-demand consolidation today.** `OvernightDaemon.run_consolidation` (read-only memory notes, FinOps-gated, OCC-safe) ships and is fired from the HUD; the deeper autonomous multi-task profiles (Big/Hybrid working ahead on a focus) ride on the deferred MCTS rollout above.
- **Orchestrator tool bundle is built; graph promotion is permanently out of scope.** Orchestrator's introspection tools (get_wbs_status, emit_hitl_request) are built, role-gated, and reachable via `core/tool_registry.py`, but the Orchestrator node itself is a deterministic O(1) function with no LLM to drive a dispatch loop (DEBT-068) — its operations remain direct state access, by design, not as a gap. The Researcher is a live tool-dispatch node (glob, grep, workspace_structure, query_graphrag, get_dependents, plus the shared perception tools) — resolved in `main.py`'s production graph, not merely optional Planner context.
- **The ~53-tool role-gated catalog is built and, since Division 8.18, fully reachable.** Twelve `register_*_tools` families populate the catalog at FastAPI startup (`main.py` lifespan → `populate_tool_catalog`). `core/tool_registry.py::resolve_tools()` bridges every selected `ToolSchema` to a live, dispatch-ready instance; a small, explicit, reasoned `_INTENTIONALLY_UNREGISTERED` set (~11 names) covers the tools deliberately left unwired — redundant with a live primitive (e.g. the mutation tools vs. the agentic cell's own `apply_granular_edit`), or owned by a separate process (the gateway package's 6 duplicate classes) — checked by a reachability gate (`tests/test_phase8_18_checkpoint_gate.py`) so a newly-added tool class can never silently go unwired again. The runtime dispatch loop (`core/tool_dispatch.py::ToolDispatcher`) is live on the Analyst, the Researcher, the Coder's iterative path (`brain/agentic_cell.py`'s additive registry fallback, gated by the same tier/RBAC/HITL machinery), the Coder's one-shot grounding pre-pass, and dispatched dev-role subagents. A HITL-tier tool the fallback branch retrieves is no longer denied outright: `ToolDispatcher.classify()` defers it through the `pending_tool_call` state channel and resolves it by exact name on resume, never re-ranked. The one deliberate asymmetry that remains: the one-shot path is capped at a READ_ONLY tier rather than reaching parity with the cell, because the `error_correction` retry loop re-enters it — a mutating call there would violate the idempotency invariant, so mutation stays exclusively the cell's surface by design, not as a gap.
- **Provider-native prompt caching is not wired — deliberately deferred, after measurement, not overlooked.** Today's caching is a *semantic/response* cache (short-circuits near-identical requests); it is **not** the same as Anthropic/OpenAI **prompt caching** (`cache_control` / ephemeral breakpoints), which gives a ~90 % discount on *input* tokens re-sent unchanged. The prerequisite shipped: the system message is split into a byte-identical HEAD (`agents/prompts.py::build_static_identity_prompt`) and a small per-turn TAIL declaring the sandbox nonce, so the prefix no longer changes on every call — guarded by `tests/test_prompt_prefix_stability.py`. Applying `cache_control` on top of it is what's deferred, because measuring the prefix disproved the assumption behind the idea: it is only **~281-450 tokens**, below every current provider's 512-4096 minimum-cacheable floor, and two of the three components originally assumed to be in it aren't — tool/MCP schemas live in a separate reasoning call rather than the stable HEAD, and GraphRAG context is assembled per-`target_file`, genuinely volatile. Tagging a sub-floor prefix would pay the 1.25× cache-write premium on every call for zero reads, a net loss. Tracked as DEBT-137 with an explicit re-evaluation trigger (a future change that folds tool schemas into the HEAD itself, or bringing the genuinely-growing multi-turn chat history into scope). Honest sizing even once unblocked: caching saves nothing on a local model, and on a cloud model the volatile per-step payload dwarfs the cacheable prefix by roughly an order of magnitude.
- **Specialized agent classes** (RefactorAgent, SecOpsAgent, …) are **roles** on `WBSStep.target_role`, not standalone modules.
- **Auth / multi-user / cloud deployment** is roadmap, not shipped.
- **The main graph's code gate is AST-only, not AST+LSP.** `brain/apply_gate.py` rejects a generated file that fails to parse before it reaches disk, but it does not lint or type-check it — no undefined-reference detection, no ruff/eslint/mypy pass. `tools/validation/lsp_filter.py` implements that layer; wiring it into this path is tracked as backlog, not silently assumed to exist. Do not read the topology diagram's `apply_commit` step as "fully linted" — it is "confirmed to parse."

If you want one of these, it's a great place to start — see [CONTRIBUTING.md](CONTRIBUTING.md) and the manifest.

---

## Design principles

1. **Local-first, cloud-when-it-helps.** The router defaults local; a token ledger quantifies the savings.
2. **Spec-driven.** The Planner produces a spec; the Coder consumes it; drift between re-plans triggers HITL.
3. **Fail fast, fail cheap.** Pydantic on every state mutation; bounded local repair before any cloud escalation; circuit breakers wherever a feedback loop can occur.
4. **Atomic writes.** Every disk artefact uses `tempfile + os.replace`.
5. **Multi-tenant by default.** Every retrieval and GC predicate carries a workspace hash.
6. **Honest telemetry.** Local vs. cloud is measured, not guessed; silent rejections are an explicit signal.
7. **Fail-closed security.** Unknown tools are DANGEROUS until allow-listed; degraded execution always requires human sign-off.

---

## Further reading

| Doc | What's in it |
| --- | --- |
| [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) | The authoritative phase-by-phase WBS and status |
| [docs/DEV_JOURNAL.md](docs/DEV_JOURNAL.md) | Active Phase 8.x engineering log + gate numbers |
| [docs/DEV_JOURNAL_ARCHIVE.md](docs/DEV_JOURNAL_ARCHIVE.md) | Compressed history — Phase 0 through 7.19 (one entry per sub-phase) |
| [docs/SCHEMA_EVOLUTION.MD](docs/SCHEMA_EVOLUTION.MD) | State and agent contracts |
| [docs/SYSTEM_PROMPTS.md](docs/SYSTEM_PROMPTS.md) | Agent system prompts |
| [docs/TECH_DEBT_BACKLOG.md](docs/TECH_DEBT_BACKLOG.md) | Tracked technical debt |
| [docs/DEBUGGING_RUNBOOK.md](docs/DEBUGGING_RUNBOOK.md) | Where to look when a real install misbehaves — exec log, timeline, audit chain, telemetry |
| [docs/GATEWAY_INTEGRATION.md](docs/GATEWAY_INTEGRATION.md) | External-agent integration guide for the MCP capability gateway (launch, auth, ceilings, catalog, versioning) |
| `docs/PHASE_*_BLUEPRINT.md` | Per-phase architectural contracts (ADRs) |
| [CLAUDE.md](CLAUDE.md) | Coding standards, architectural guardrails, and build protocols |
