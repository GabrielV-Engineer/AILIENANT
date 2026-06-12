# AILIENANT — Developer Guide (Internal)

> **Audience:** core contributors working *on* AILIENANT. This is the deep technical reference — architecture, the execution graph, subsystem internals, pseudocode for the load-bearing paths, the security model, and an honest map of what is and isn't built. If you're a *user*, start with [HowToUseIt.md](HowToUseIt.md); for a gentle architectural tour, see [HowItWorks.md](HowItWorks.md).
>
> **Source of truth for status & roadmap:** [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) and [docs/DEV_JOURNAL.md](docs/DEV_JOURNAL.md). Where this guide and those disagree, they win. Architectural contracts live in the `docs/PHASE_*_BLUEPRINT.md` files and `docs/SCHEMA_EVOLUTION.MD`.

---

## Contents

- [System shape](#system-shape)
- [The execution graph](#the-execution-graph)
- [The two agents](#the-two-agents)
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
       no  → planner_agent
               → drift_monitor              (compare to immutable_wbs)
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

## The two agents

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

For steps the planner flags as needing iteration, control routes into the **agentic cell** ([brain/agentic_cell.py](ailienant-core/brain/agentic_cell.py)): a bounded ReAct loop over a live, persistent terminal exposing exactly three strict-schema tools — `run_terminal` (structured diagnostics, never raw stdout), `read_file_ast` (skeleton, not full file), and `apply_granular_edit` (transactional SEARCH/REPLACE with an optimistic-concurrency guard).

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

`dump_state_to_markdown` ([core/state_manager.py](ailienant-core/core/state_manager.py)) writes a human-readable checkpoint with an embedded machine-JSON payload to `<workspace>/.ailienant/AGENTS.md` via temp-file + `os.replace`. `load_state_from_markdown` returns `None` if missing or older than `max_age_seconds` (default 3600). On a warm hit, the planner skips the LanceDB embedding call and runs only `deep_parse`.

### Memory Janitor

[core/janitor.py](ailienant-core/core/janitor.py): vector GC drops LanceDB rows (filtered by `workspace_hash`) whose source file no longer exists; graph purge deletes obsolete pruned MCTS episodes. Triggered via `POST /api/v1/system/janitor`.

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
│   │   ├── routing_engine.py    #     CSS × TCI matrix
│   │   ├── agentic_cell.py      #     bounded ReAct cell + contained MCTS candidate selection
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
│   │   ├── state_manager.py     #     AGENTS.md fast-boot serializer
│   │   ├── janitor.py           #     orphan-vector GC + MCTS purge
│   │   ├── token_ledger.py      #     LOCAL/CLOUD token accounting
│   │   ├── memory/              #     semantic, trajectory, graphrag_extractor, context_auditor,
│   │   │                        #     docs_index (product-docs RAG — reserved LanceDB namespace)
│   │   ├── readme_digest.py     #     workspace README brain: verbatim/digest/head-slice + debounced rebuild
│   │   ├── db.py                #     SQLite catalog (dependency_graph, ppr_scores, indexed_files)
│   │   └── config/              #     BYOM schema + embedding/model resolvers + profiles
│   │       ├── mcp_secrets.py   #       backend-masked MCP credential store (0600) + connect-time env injection
│   │       └── host_discovery.py #      ephemeral ~/.ailienant/run.json (port+token+pid, 0600) + async liveness probe
│   ├── api/                     #   WS manager + REST routers (memory, byom, hardware, audit,
│   │                            #     mcp_servers, skills, sessions, agent_roles, system_settings)
│   ├── tools/                   #   llm_gateway, validation pipeline (AST+LSP), MCP adapter
│   │                            #     (multi-session registry + dispatch gate), perception/
│   │                            #     mutation/execution/control tool bundles,
│   │                            #     validation/diagnostics.py (structured verdict parser)
│   ├── gateway/                 #   External Capability Gateway: stdio MCP server exposing AILIENANT
│   │                            #     verbs to external agents (catalog + schemas + call-tool routing seam)
│   │   ├── governance.py        #     symmetric permission gate (evaluate_action reuse) + caller_id +
│   │   │                        #       conservative posture (no self-escalation, no silent AUTO)
│   │   └── ledger.py            #     durable per-caller token-bucket + budget DoS guard (filelock, fail-closed)
│   ├── transport/               #   outbound WS stream (throttler, token batcher, narration gate)
│   ├── shared/                  #   config, RBAC, contracts, hardware probe, persona, log filters
│   ├── validators/              #   syntax/style gates (ast.parse + ruff --stdin), env probe
│   └── tests/                   #   pytest suite + per-phase checkpoint gates + chaos crucible
├── ailienant-extension/         # VS Code extension (TypeScript + React)
│   ├── src/
│   │   ├── extension.ts         #     activation entry
│   │   ├── ide_sync.ts          #     context capture (debounced, .ailienantignore gate)
│   │   ├── webview/             #     React sidebar (chat, ThoughtBox, diffs, HUD, checklist)
│   │   ├── dashboard/           #     Web Dashboard SPA (Hardware/BYOM/Rules/Staging/Audit/…)
│   │   ├── core/                #     IntentRouter, PatchActuator, tokenizer, inline-edit manager
│   │   ├── workspace/ · sidebar/ #    Zustand stores, streaming markdown parser
│   │   ├── providers/ · api/    #     chat provider, WS client, path index, HITL notifier
│   │   └── test/                #     vscode-test mocha suite
│   ├── media/                   #   source logos (logo.svg, icon-color.svg, icon.svg)
│   └── esbuild.js               #   3 build contexts (extension CJS · webview IIFE · dashboard ESM)
├── docs/                        # Manifest, blueprints, dev journal, schema, system prompts, tech debt
├── README.md  (+ 6 translations)# Public landing page
├── HowToUseIt.md · HowItWorks.md# User & architecture guides
├── DEVELOPERS.md                # This document
├── CONTRIBUTING.md · CLA.md     # Contribution guide + CLA
├── LICENSE · LICENSING.md       # AGPL-3.0 + dual-license explainer
└── CLAUDE.md · AGENTS.md        # Operating rules for AI contributors
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
| `GET /api/v1/memory/{sections,graph,vectors}` | GraphRAG browse surfaces |
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

The suite is large (latest gate: **1,103 passing / 2 skipped**, `mypy .` clean — see the most recent [DEV_JOURNAL.md](docs/DEV_JOURNAL.md) entry for the current numbers). Each phase ships a sibling **checkpoint-gate** test file (`test_phase*_checkpoint_gate.py`) that re-certifies that phase's contract.

**Zero-degradation rule:** your change must not introduce a single new type error or lint warning. **Boy-Scout rule:** if a file you touch already has errors, fix them while you're there.

---

## Honest list of what is NOT implemented

Documentation should never oversell. As of this writing:

- **MCP dispatch wiring is substantially complete.** Auto-connect on server startup (idempotent multi-session registry, teardown wired into FastAPI lifespan) and the `evaluate_action` dispatch guard in `McpToolAdapter._arun` (DENY/HITL/ALLOW per the privilege matrix; READ_ONLY friction-free; catalog overrides bind live at harvest) are shipped as of 8.4.4. Remaining: trust-once session-scoped valve, live e2e dispatch from the graph cell, and FE HITL-card binding for `MCP_TOOL_CALL` — all deferred to 8.4.7. Live Skills execution is tracked separately as 8.4.5.
- **Wasm sandbox tier is built but not the production default.** The resolver prefers Docker; Wasm is a pure-compute fallback. gVisor-class isolation is not present.
- **Full MCTS rollout is deferred.** The tree, UCB1 selection, and pruning exist; the only *live* MCTS edge is the contained candidate-selection inside the agentic cell. The offline rollout loop is future work.
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
| [docs/DEV_JOURNAL.md](docs/DEV_JOURNAL.md) | Per-phase engineering log + gate numbers |
| [docs/SCHEMA_EVOLUTION.MD](docs/SCHEMA_EVOLUTION.MD) | State and agent contracts |
| [docs/SYSTEM_PROMPTS.md](docs/SYSTEM_PROMPTS.md) | Agent system prompts |
| [docs/TECH_DEBT_BACKLOG.md](docs/TECH_DEBT_BACKLOG.md) | Tracked technical debt |
| `docs/PHASE_*_BLUEPRINT.md` | Per-phase architectural contracts (ADRs) |
| [CLAUDE.md](CLAUDE.md) | Operating rules for AI contributors |
