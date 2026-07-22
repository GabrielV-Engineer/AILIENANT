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

The entry point is `process_user_intent(prompt, mode)` in [ailienant-core/brain/intent_router.py](ailienant-core/brain/intent_router.py), which dispatches one of three execution shapes:

```
process_user_intent(prompt, mode)
  SEQUENTIAL  → fast_path.execute_sequential_bypass()      # zero-graph, 1–3 s
  MICRO_SWARM → swarms._MICRO_SWARM_APP.ainvoke()          # Coder ↔ SyntaxGate ↔ StyleGate ↔ CircuitBreaker
  FULL_SWARM  → swarms.build_full_swarm(checkpointer).ainvoke()
```

The full graph is compiled in [ailienant-core/brain/engine.py](ailienant-core/brain/engine.py) over a strictly-typed `AIlienantGraphState` ([brain/state.py](ailienant-core/brain/state.py)):

```
START
  → summarize_history
  → session_delta_aggregator
  → [planner_mode_active?]
       yes → ideation_loop  (Socratic clarification)         → END (suspend on HITL)
       no  → researcher_agent  (owns retrieval + routing cascade; emits skeleton + context_metrics)
               → planner_agent  (pure WBS engine; consumes the routing signal)
               → drift_compute → drift_gate (compare to immutable_wbs; HITL via native interrupt/resume)
                 → route_to_coders          (SWARM if cloud, RELAY if local)
                   → coder_agent (×N parallel in cloud)
                     → contract_guard       (assert workspace state before write)
                       → finops_gate        (cost < ceiling?)
                         → supervisor_node   (FinOps hard-kill or proposal)
                           → apply_patch
                             → validate_output (AST + LSP)
                               → [retry / heal?] → coder_agent | error_correction | agentic_cell | END
```

Every node transition is persisted by a `HybridCheckpointer` over SQLite WAL, so every super-step is **durable, resumable, and branchable** (time-travel). The conditional loop-back edges (`route_after_coder`, `route_after_cell`) are what turn each repair iteration into its own checkpoint.

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
- Freezes an `immutable_wbs` on the first plan; the **drift monitor** flags semantic divergence on any re-plan and escalates to HITL.
- Routes via the CSS × TCI matrix + Mini-Judge veto (see [Hybrid routing](#hybrid-routing)).
- Its system prompt enforces scope discipline (touch only named/necessary files) and polyglot-file safety (SEARCH/REPLACE only on mixed-syntax files).

### Coder — [agents/coder.py](ailienant-core/agents/coder.py)

Takes one WBS step and emits a patch as git-conflict-style SEARCH/REPLACE blocks (never JSON-escaped). Validation happens on a virtual overlay before anything hits disk:

- **AST parse** (Tree-sitter, 20+ grammars) → fast, language-agnostic syntax gate.
- **LSP lint** (subprocess to ruff/eslint/mypy/…) → catches undefined refs and lints.
- Bounded local retries; on the configured strike count it escalates to a cloud "surgeon."
- `run_command` steps dispatch into the resolved sandbox tier and read a **structured** verdict — see [the closed-loop executor](#closed-loop-execution).

For steps the planner flags as needing iteration, control routes into the **agentic cell** ([brain/agentic_cell.py](ailienant-core/brain/agentic_cell.py)): a bounded ReAct loop over a live, persistent terminal ([core/pty_session.py](ailienant-core/core/pty_session.py) — one long-lived shell owns `cwd`/`env`, async byte-stream with backpressure, Ctrl-C, teardown) exposing exactly three strict-schema tools — `run_terminal` (structured diagnostics, never raw stdout), `read_file_ast` (skeleton, not full file), and `apply_granular_edit` (transactional SEARCH/REPLACE with an optimistic-concurrency guard).

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

Tools are role-gated `ToolSchema`s in a RAM-resident LanceDB store ([core/tool_rag.py](ailienant-core/core/tool_rag.py), `ToolRAGStore`): each declares a `ToolPrivilegeTier` and an `allowed_roles` frozenset, enforced at dispatch. Registered via `register_{perception,mutation,execution,control}_tools` (16 tools today); MCP tools are harvested into the same store at session bootstrap. The roadmap to ~56 role-assigned tools — and the `tool_search` deferred-loader that keeps a large catalog inside the prompt budget — is [División 8.8](docs/PROJECT_MANIFEST.md).

### Closed-loop execution

The reliability core. A `run_command` step dispatches into `core.sandbox.ACTIVE_ADAPTER` and reads the **typed** `SandboxResult.exit_code` — never string-sniffed from stdout:

```python
result = await get_active_adapter().execute(cmd, timeout_s=…, cwd=…, env_whitelist=…)
diagnostics = parse_diagnostics(result)          # tools/validation/diagnostics.py — total, never raises
if result.exit_code == 0:
    state["step_status"] = "completed"
else:
    # emit a reflexion-mimicking healing delta; route_after_coder carries it to error_correction
    state["healing_required"] = format_diagnostics(diagnostics)   # bounded
# correction budget concedes rather than looping forever
```

If no adapter resolved, the step is honestly surfaced as deferred (`EXECUTE_TIER_DEFERRED`) rather than falsely "completed."

### State management

`AIlienantGraphState` ([brain/state.py](ailienant-core/brain/state.py)) is a strict `TypedDict` with custom reducers for parallel fan-out keys (`vfs_buffer`, `generated_code`, `current_cost_usd`). The first planner turn freezes `immutable_wbs`; the drift monitor diffs every later plan against it.

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

Docker is the daemon-pattern default (persistent `ailienant-sandbox-daemon`, exec socket, `_DockerPtyBackend` for interactive sessions). Wasm is fuel-metered with a module-import scope guard. NativeHITL is the degraded fallback — every run requires human sign-off.

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
├── .github/workflows/           # CI: builds + pushes the sandbox image to GHCR
├── ailienant-core/              # Python orchestration engine
│   ├── main.py                  #   FastAPI app + WebSocket gateway + lifespan (sandbox resolve)
│   ├── agents/                  #   Graph nodes: planner, coder, analyst, logic, researcher,
│   │                            #     orchestrator, contract_guard, error_correction, inline_edit,
│   │                            #     workspace_context, analyst_context, recency
│   ├── brain/                   #   State machine + routing + checkpointing
│   │   ├── engine.py            #     graph assembly + reflexion/self-heal + agentic-cell wiring
│   │   ├── intent_router.py     #     process_user_intent() dispatch
│   │   ├── swarms.py            #     micro / full swarm builders
│   │   ├── fast_path.py         #     SEQUENTIAL bypass
│   │   ├── state.py             #     AIlienantGraphState, MissionSpecification, reducers
│   │   ├── subagent_contracts.py #    dispatch schema: SubagentTask / DispatchPlan / result envelopes
│   │   ├── dispatch.py          #     build_dispatch_sends fan-out + wave-split routers
│   │   ├── dispatch_ledger.py   #     budget admission: reserve/commit/refund over current_cost_usd
│   │   ├── dispatch_emitter.py  #     optional planner/researcher DispatchPlan emission (hook + synthetic)
│   │   ├── nodes/               #     subagent_worker + dispatch_synthesize dispatch nodes
│   │   ├── routing_engine.py    #     CSS × TCI matrix
│   │   ├── context_pipeline.py  #     5-layer context assembler (ContextChunk, ContextPipeline)
│   │   ├── agent_context.py     #     budget-guard over ContextPipeline (build_agent_context)
│   │   ├── agentic_cell.py      #     bounded ReAct cell (re-exports run_tournament as select_candidate_via_mcts)
│   │   ├── subagent_tournament.py #   transactional UCB1 candidate tournament + run_tournament_from_dispatch adapter
│   │   ├── iteration_governor.py #    multi-axis circuit breaker
│   │   ├── retry_policy.py      #     centralized retry/correction budgets
│   │   └── mcts/ · episodic/    #     tree + UCB1 + audit checkpointer
│   ├── core/                    #   Infrastructure
│   │   ├── sandbox.py           #     SandboxAdapter ABC + Docker/Wasm/NativeHITL + resolver
│   │   ├── pty_session.py       #     persistent interactive shell sessions (PTY)
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
│   │   ├── janitor.py           #     orphan-vector GC + MCTS purge
│   │   ├── token_ledger.py      #     LOCAL/CLOUD token accounting
│   │   ├── graph_weight.py      #     pre-execution context-OOM predictor (state tokens vs candidate window)
│   │   ├── observability.py     #     env-gated LangSmith tracing bootstrap (no new sink)
│   │   ├── deferred_tool_loader.py # eager-vs-deferred tool injection over ToolRAGStore (~10%-budget gate)
│   │   ├── tool_dispatch.py     #     runtime tool-dispatch loop (gated, self-correcting; live on Analyst)
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
│   │       └── host_discovery.py #      ephemeral ~/.ailienant/run.json (port+token+pid, 0600) + async liveness probe
│   ├── api/                     #   WS manager + REST routers (memory, byom, hardware, audit,
│   │                            #     mcp_servers, skills, sessions, agent_roles, system_settings);
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
│   ├── validators/              #   syntax/style gates (ast.parse + ruff --stdin), env probe
│   └── tests/                   #   pytest suite + per-phase checkpoint gates + chaos crucible
│       ├── e2e/                 #     real HTTP/WS end-to-end (SSoT prompt→graph→WS→applied patch)
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
│   │   ├── webview/             #     React sidebar (chat, ThoughtBox, diffs, HUD, checklist)
│   │   ├── dashboard/           #     Web Dashboard SPA (grouped/collapsible nav shell + panels)
│   │   │   ├── panels/          #       Hardware/BYOM/Rules/Staging/Audit/Overview/Memory/…
│   │   │   │   └── memory/      #         GraphRAG viz: CodeGraphLayer (2D), VectorMapLayer, EmbeddingBrowser
│   │   │   │       └── nebula/  #           custom three.js "Neural Nebula" 3D engine (lazy-split)
│   │   │   ├── ui/              #       design-system primitives (Card, StatTile, Button, Badge, Skeleton, EmptyState, ShortcutsOverlay, ConfirmModal)
│   │   │   └── hooks/           #       usePollingWhileVisible · useSidebarCollapsed · useKeyboardShortcuts
│   │   ├── core/                #     IntentRouter, PatchActuator, tokenizer, inline-edit manager
│   │   ├── workspace/           #    Zustand stores, streaming markdown parser
│   │   │   ├── chatStore.ts     #      memory-only Zustand store — 22 live chat-runtime fields (messages, streaming, toasts, …)
│   │   │   ├── workspaceStore.ts #     persisted Zustand store — UI slice (mode, preset, inflightTurn, drafts, …)
│   │   │   ├── types.ts         #      shared message type exports (ConversationMessage, SystemMessage, Message, NattMessage, …)
│   │   │   ├── Workspace.tsx    #      layout host — selectors + hook calls; <800 lines
│   │   │   ├── hooks/
│   │   │   │   ├── useWSMessageHandler.ts #  no-arg WS dispatch controller (45-branch switch, rAF buffers, watchdog)
│   │   │   │   └── useSessionPersistence.ts # PERSIST_TRANSCRIPT + inflight snapshot + mount-rehydrate effects
│   │   │   ├── components/
│   │   │   │   └── ToastStack.tsx #    presentational toast list (JSX, role="alert" per item)
│   │   │   └── utils/
│   │   │       └── messageDispatchHelpers.ts # pure helpers + dispatch consts (mkId, mergeById, attachOrUpdate*, …)
│   │   ├── sidebar/             #    sidebar webview
│   │   ├── providers/ · api/    #     chat provider, WS client, path index, HITL notifier,
│   │   │                        #     devcontainerProvisioner.ts (vscode-free lifecycle driver — probe/up/exec/degrade),
│   │   │                        #     devcontainerFactory.ts (vscode wiring + lazy singleton),
│   │   │                        #     devcontainerExecHandler.ts (host-side bridge handler — provision/exec, streams back),
│   │   │                        #     devcontainerScaffold.ts (idempotent .devcontainer/devcontainer.json starter)
│   │   └── test/                #     vscode-test mocha suite
│   ├── media/                   #   source logos (logo.svg, icon-color.svg, icon.svg)
│   └── esbuild.js               #   3 build contexts (extension CJS · webview IIFE · dashboard ESM)
├── docs/                        # Manifest, blueprints, dev journal, schema, system prompts, tech debt
├── README.md  (+ 6 translations)# Public landing page
├── HowToUseIt.md · HowItWorks.md# User & architecture guides
├── DEVELOPERS.md                # This document
├── CONTRIBUTING.md · CLA.md     # Contribution guide + CLA
├── LICENSE · LICENSING.md       # AGPL-3.0 + dual-license explainer
└── CLAUDE.md · AGENTS.md        # Coding standards, architectural guardrails, and build protocols
```

> Keep this tree accurate. Per [CLAUDE.md](CLAUDE.md) §5, any new file or structural directory must be reflected here.

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

The suite is large (latest gate: **2,022 passing / 2 skipped**, `mypy .` clean — see the most recent [DEV_JOURNAL.md](docs/DEV_JOURNAL.md) entry for the current numbers). Each phase ships a sibling **checkpoint-gate** test file (`test_phase*_checkpoint_gate.py`) that re-certifies that phase's contract.

**Zero-degradation rule:** your change must not introduce a single new type error or lint warning. **Boy-Scout rule:** if a file you touch already has errors, fix them while you're there.

---

## Honest list of what is NOT implemented

Documentation should never oversell. As of this writing:

- **MCP dispatch wiring is shipped at the adapter layer.** Auto-connect on server startup (idempotent multi-session registry, teardown wired into FastAPI lifespan) and the `evaluate_action` dispatch guard in `McpToolAdapter._arun` (DENY/HITL/ALLOW per the privilege matrix; READ_ONLY friction-free; catalog overrides bind live at harvest) are shipped. What remains: trust-once session-scoped valve (DEBT-029, floating) and FE HITL-card binding for `MCP_TOOL_CALL` (floating). The runtime LLM tool-dispatch loop that actually invokes registered tools from within the graph now exists and is live on the Analyst; extending it to the remaining roles is tracked as DEBT-068 (see the tool catalog entry below).
- **Wasm sandbox tier is built but not the production default.** The resolver prefers Docker; Wasm is a pure-compute fallback. gVisor-class isolation is not present.
- **Full MCTS rollout is deferred.** The tree, UCB1 selection, and pruning exist; the only *live* MCTS edge is the contained candidate-selection inside the agentic cell. The offline rollout loop is future work.
- **Dreaming is on-demand consolidation today.** `OvernightDaemon.run_consolidation` (read-only memory notes, FinOps-gated, OCC-safe) ships and is fired from the HUD; the deeper autonomous multi-task profiles (Big/Hybrid working ahead on a focus) ride on the deferred MCTS rollout above.
- **Researcher and Orchestrator tool bundles are built; graph promotion is the remaining step.** Researcher's five READ_ONLY tools (glob, grep, workspace_structure, query_graphrag, get_dependents) and Orchestrator's introspection tools (get_wbs_status, emit_hitl_request) are built and role-gated; their state-injecting factories are wired. Researcher's output is still consumed as optional Planner context rather than from a standalone graph node, and the Orchestrator's operations are still direct state access. Promotion of both to first-class graph nodes rides on the DEBT-068 dispatch-loop extension. The Analyst (six READ_ONLY tools: run_linter, analyze_complexity, audit_dependencies, diff_changes, web_search, read_token_ledger) is the first role wired to the live tool-dispatch loop; the Planner and Coder are the fully-wired cognitive core.
- **The 56-tool role-gated catalog is built; the cognitive dispatch loop is live on one role.** Six tool waves shipped ~34 net-new role-gated tools (Researcher, Analyst, Orchestrator, Planner, Coder, Gateway/Benchmark, Universal) on top of the original 16, bringing the registered store to ~50 tools with `tool_search` deferred-loading at the 10%-budget gate. Schema definitions, state-injecting factories, and RBAC assignments are all present and correct. The runtime dispatch substrate now exists — `core/tool_dispatch.py` (`ToolDispatcher`, prompt-enforced-JSON parsing, self-correcting loop, `evaluate_action`-gated) — and is **wired live on the Analyst node**, whose tools are all READ_ONLY. Extending the loop to the Coder/Planner/Orchestrator (plus human-in-the-loop routing for mutating tiers, and promoting the Researcher to a graph node) is the remaining gap, tracked as DEBT-068.
- **Provider-native prompt caching is not wired (a large pending cost win); planned for the pre-launch innovation sprint.** Today's caching is a *semantic/response* cache (short-circuits near-identical requests); it is **not** the same as Anthropic/OpenAI **prompt caching** (`cache_control` / ephemeral breakpoints), which gives a ~90 % discount on *input* tokens that are re-sent unchanged. Our payload is ideal for it — the system prompts, MCP tool schemas, and GraphRAG context are large and static across a graph iteration. The work: structure the LangGraph message assembly so that stable, high-volume prefix (system prompt → tool/MCP schemas → GraphRAG/manifest context) is **byte-identical and front-loaded** ahead of the volatile per-step suffix, then tag the cache breakpoint, so every coder/planner iteration re-reading the same context hits the cache instead of re-billing it. This is a transport-layer change in [tools/llm_gateway.py](ailienant-core/tools/llm_gateway.py) + the prompt builders; it touches no agent logic.
- **Specialized agent classes** (RefactorAgent, SecOpsAgent, …) are **roles** on `WBSStep.target_role`, not standalone modules.
- **Auth / multi-user / cloud deployment** is roadmap, not shipped.

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
| [docs/GATEWAY_INTEGRATION.md](docs/GATEWAY_INTEGRATION.md) | External-agent integration guide for the MCP capability gateway (launch, auth, ceilings, catalog, versioning) |
| `docs/PHASE_*_BLUEPRINT.md` | Per-phase architectural contracts (ADRs) |
| [CLAUDE.md](CLAUDE.md) | Coding standards, architectural guardrails, and build protocols |
