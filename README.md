# AILIENANT

> A hybrid agentic orchestrator for software engineering — local-first, cost-aware, and IDE-native.

AILIENANT is a Python orchestration engine paired with a thin VS Code extension that brings autonomous coding agents into the editor while keeping latency, cost, and privacy under explicit control. It runs a LangGraph state machine, hybrid local/cloud LLM routing, a multi-tenant memory layer (LanceDB + SQLite WAL), and a Monte Carlo Tree Search scaffold for offline exploration.

---

## Table of contents

- [Status](#status)
- [What it actually does today](#what-it-actually-does-today)
- [Architecture at a glance](#architecture-at-a-glance)
- [Repository layout](#repository-layout)
- [Tech stack](#tech-stack)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Core subsystems](#core-subsystems)
- [Roadmap](#roadmap)
- [Testing and quality gates](#testing-and-quality-gates)
- [Design principles](#design-principles)
- [Honest list of what is NOT implemented](#honest-list-of-what-is-not-implemented)
- [Contributing](#contributing)
- [License](#license)

---

## Status

**Current phase:** **3.7 — Checkpoint Gate** (complete). The system has just finished a cross-subsystem verification pass covering retrieval, context cascade, hybrid routing, memory janitor, and cognitive fast-boot.

| Metric | Value |
| --- | --- |
| Backend tests passing | **283** |
| `mypy --strict` on new modules | Clean |
| `ruff check` | Clean |
| Concurrent SQLite WAL reads (50 tasks) | No lock errors, p95 < 250 ms |
| Token-ledger isolation (LOCAL strict run) | `cloud_tokens == 0.0` |

The next planned milestone is **Phase 4** — promoting the CoderAgent stub to a real tool-using executor and wiring the MCP adapter.

---

## What it actually does today

- **Spec-Driven Planning.** The PlannerAgent turns a user prompt into a strict `MissionSpecification` (outcome, scope, constraints, decisions, WBS tasks, acceptance checks). The first plan is frozen as an `immutable_wbs` and a DriftMonitor catches semantic drift on replans.
- **Smart hybrid routing.** A 2D matrix of Context Sufficiency Score (CSS) and Task Complexity Index (TCI), combined with a cheap Mini-Judge LLM, picks `LOCAL_SMALL` / `LOCAL_BIG` / `CLOUD`. A red-alert path bypasses the judge when context is too thin. A MEDIUM judge verdict escalates `LOCAL_SMALL` → `LOCAL_BIG`; a HIGH verdict vetoes to `CLOUD`.
- **GraphRAG retrieval.** A single embedding call hits LanceDB for top-K files, then a 1-hop SQLite dependency expansion is parsed with Tree-sitter inside `asyncio.to_thread`. The depth `k`, file count, and token ceiling scale with the routing tier.
- **Multi-tenant isolation.** Every retrieval query is pre-filtered by `workspace_hash = sha256(workspace_root)`. The Janitor and the LanceDB store enforce the same key. Single-quote SQL escaping and a strict allowlist regex prevent injection.
- **Cognitive Fast-Boot.** After each successful plan, the planner flushes mission state to `.ailienant/AGENTS.md` (atomic temp-file + `os.replace`, with a `<!-- MACHINE_DATA_JSON ... -->` payload). On the next cold start, if the file is < 1 hour old, the LanceDB embedding call is skipped entirely.
- **Memory Janitor.** A janitor sweep (`POST /api/v1/system/janitor`) deletes LanceDB vectors whose source files no longer exist on disk and purges old pruned MCTS episodes from `ailienant_mcts.sqlite`.
- **Hybrid MCTS Fixer Loop.** Generated code passes through a validation pipeline (AST + LSP). On failure, up to three `Tier.LOCAL` repair attempts run; the fourth strike trips an implicit circuit breaker (`MCTSNode.error_streak >= 3`) and escalates to a `Tier.CLOUD` "surgeon" call. The token ledger tracks LOCAL vs CLOUD usage and an estimated savings figure.
- **Spec-Driven HITL.** The FinOps gate halts execution at a configurable `max_budget_usd` ceiling; the IdeationLoop (when `planner_mode_active=True`) runs a Socratic clarification dialogue before any code is generated.
- **Telemetry & Rule Distillation.** When the user edits ≥ 70 % of an AI-merged code block within 3 minutes, the extension reports a silent rejection. The backend distills rejection patterns into rules persisted under `.ailienant/rules/`.

The VS Code extension is intentionally **thin**: a sidebar with a master toggle and an intelligence profile selector, a VFS reader that captures dirty buffers, a WebSocket client, an MCTS "dream diff" viewer, and a silent rejection telemetry channel. The dashboard, agent picker, and graph visualizer described in earlier proposals do **not** exist yet (see [Honest list of what is NOT implemented](#honest-list-of-what-is-not-implemented)).

---

## Architecture at a glance

```
┌────────────────────────────────────┐         ┌──────────────────────────────────────┐
│  VS Code Extension (TypeScript)    │         │  ailienant-core (Python, FastAPI)    │
│  ──────────────────────────────    │         │  ──────────────────────────────────  │
│  • Sidebar webview (React)         │         │  • FastAPI app + WebSocket gateway   │
│    - Master toggle                 │  HTTP   │  • LangGraph state machine            │
│    - Intelligence profile picker   │ ──────► │  • Hybrid LLM router (CSS × TCI)     │
│  • VFS reader (dirty buffers)      │  WS     │  • GraphRAG retrieval                 │
│  • MCTS Mirror diff viewer         │ ◄────── │  • MCTS scaffold + Fixer Loop        │
│  • Silent rejection telemetry      │         │  • Memory Janitor + Fast-Boot        │
│  • Intent router (local shortcuts) │         │  • Token Ledger + FinOps gate         │
└────────────────────────────────────┘         └────────────────┬─────────────────────┘
                                                                │
                                       ┌────────────────────────┼──────────────────────┐
                                       │                        │                       │
                                ┌──────▼──────┐         ┌───────▼───────┐      ┌────────▼────────┐
                                │  LanceDB    │         │  SQLite WAL   │      │ LiteLLM proxy   │
                                │  vectors    │         │  catalog +    │      │ (local + cloud) │
                                │  (HNSW)     │         │  MCTS audit   │      │                 │
                                └─────────────┘         └───────────────┘      └─────────────────┘
```

The LangGraph flow (built in [ailienant-core/brain/engine.py](ailienant-core/brain/engine.py)):

```
START
  → summarize_history
  → session_delta_aggregator
  → [planner_mode_active ?]
      yes → ideation_loop → END
      no  → planner_agent
              → drift_monitor
                → route_to_coders          (SWARM if CLOUD, RELAY if LOCAL)
                  → coder_agent (× N parallel)
                    → finops_gate
                      → apply_patch
                        → validate_output
                          → [retry?] → coder_agent OR → END
```

State is checkpointed by a `HybridCheckpointer` over SQLite WAL — every node transition is durable, enabling time-travel debugging and resume-after-crash.

---

## Repository layout

```
Proyect_Ailienant/
├── ailienant-core/             # Python orchestration engine
│   ├── main.py                 # FastAPI app + WebSocket gateway
│   ├── agents/                 # LangGraph nodes (planner, coder, analyst, logic, mcts_coder, contract_guard, researcher)
│   ├── brain/                  # State machine + MCTS + checkpointing
│   │   ├── engine.py           #   graph builder
│   │   ├── state.py            #   AIlienantGraphState, ContextMeter, MissionSpecification
│   │   ├── mcts/               #   tree + registry
│   │   ├── episodic/           #   MCTS audit checkpointer
│   │   └── routing_engine.py   #   CSS × TCI matrix
│   ├── core/                   # Infrastructure
│   │   ├── db.py               #   SQLite catalog (dependency_graph, ppr_scores, indexed_files)
│   │   ├── memory/             #   semantic, trajectory, graphrag_extractor, context_auditor
│   │   ├── vfs_middleware.py   #   in-memory VFS proxy with 3-layer firewall
│   │   ├── state_manager.py    #   AGENTS.md fast-boot serializer
│   │   ├── janitor.py          #   orphan-vector GC + MCTS purge
│   │   ├── token_ledger.py     #   LOCAL/CLOUD token accounting
│   │   └── rules.py            #   .ailienant rule manager
│   ├── api/                    # WebSocket manager + MCTS mirror endpoints
│   ├── tools/                  # LLM gateway, validation pipeline (AST + LSP), MCP adapter stub
│   ├── shared/                 # Config, RBAC, contracts, hardware probe
│   └── tests/                  # 283 passing tests
├── ailienant-extension/        # VS Code extension (TypeScript + React)
│   ├── src/
│   │   ├── extension.ts        #   activation entry
│   │   ├── providers/          #   chat sidebar, MCTS mirror, telemetry
│   │   ├── webview/            #   React UI (MasterToggle, ProfileSelector)
│   │   ├── api/                #   WS + HTTP clients
│   │   ├── editor/             #   vfs_reader (dirty buffer capture)
│   │   └── core/               #   IntentRouter, PathResolver
│   ├── package.json
│   └── esbuild.js
├── docs/
│   ├── PROJECT_MANIFEST.md     # Phase-by-phase roadmap (load-bearing)
│   ├── PHASE_4_BLUEPRINT.md    # Master architectural contract for Phase 4 (mandatory read while Phase 4 is active)
│   ├── SCHEMA_EVOLUTION.MD     # State + agent contracts
│   ├── SYSTEM_PROMPTS.md       # Agent system prompts
│   ├── DEV_JOURNAL.md          # Per-phase engineering log
│   └── architecture_prompt.md  # Directory rules
├── CLAUDE.md                   # Operating instructions for AI contributors
├── .env.example
└── README.md
```

---

## Tech stack

**Backend (`ailienant-core/`)**

| Layer | Library / version |
| --- | --- |
| Orchestration | `langgraph 1.1.6`, `langchain-core 1.2.26`, `langsmith 0.7.25` |
| LLM proxy | `litellm >= 1.40` (sits in front of OpenAI, Anthropic, Google, DeepSeek, Mistral, Ollama, vLLM, llama.cpp) |
| Vector store | `lancedb 0.30.2` + `pyarrow 23.0.1` (HNSW, cosine, IVF) |
| Catalog DB | `aiosqlite >= 0.19` over SQLite in WAL mode |
| AST parsing | `tree-sitter >= 0.23` with 22 language grammars |
| API | `fastapi 0.135`, `uvicorn 0.43`, `httpx 0.28` |
| Validation | `pydantic 2.12`, `pydantic-settings 2.13` |
| Tokenization | `tiktoken` (cl100k_base) |
| Graph math | `networkx 3.6` |
| Tooling | `ruff`, `mypy`, `pytest`, `pytest-anyio` |

Python ≥ 3.10. Tested on 3.13.

**Extension (`ailienant-extension/`)**

| Layer | Library / version |
| --- | --- |
| Language | TypeScript 5.9 (strict) |
| UI | React 18.3 |
| Bundler | esbuild 0.27 |
| Transport | `ws 8.20`, native `fetch` |
| Lint | ESLint 9 + typescript-eslint |

---

## Quick start

### Prerequisites

- Python 3.10+ (3.13 recommended)
- Node.js 20+
- One or both of:
  - A local LiteLLM proxy reachable at `http://localhost:4000` (recommended — see [docs.litellm.ai](https://docs.litellm.ai/docs/simple_proxy)), OR
  - Cloud API keys set in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …)
- VS Code 1.85+ (only required to run the extension)

### 1. Backend

```powershell
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix: source venv/bin/activate
pip install -r requirements.txt

# Copy and edit env vars
copy ..\.env.example ..\.env     # Unix: cp ../.env.example ../.env

# Launch the orchestration server
uvicorn main:app --reload --port 8000
```

The server exposes:

| Route | Purpose |
| --- | --- |
| `GET /` | Health probe |
| `GET /api/v1/models/available` | Discovered model tiers |
| `POST /api/v1/task/submit` | Submit a coding task (returns `task_id`) |
| `WS /api/v1/ws/{client_id}` | Streaming events (tokens, graph mutations, telemetry) |
| `GET /api/v1/mcts/{node_id}/vfs` | Read a virtual file from an MCTS node |
| `POST /api/v1/mcts/{node_id}/merge` | Apply a stable MCTS branch to disk |
| `POST /api/v1/telemetry/reject` | Report a silent rejection (extension uses this) |
| `GET /api/v1/telemetry/tokens` | Snapshot the token ledger |
| `POST /api/v1/system/janitor` | Trigger the memory janitor (vector GC + MCTS purge) |

### 2. Extension

```powershell
cd ailienant-extension
npm install
npm run compile
```

Then in VS Code: **F5** to launch an Extension Development Host, or run `vsce package` to build a `.vsix`.

### 3. Run the tests

```powershell
cd ailienant-core
.\venv\Scripts\pytest.exe        # 270 passing
.\venv\Scripts\mypy.exe core\janitor.py core\state_manager.py --strict --explicit-package-bases
.\venv\Scripts\ruff.exe check .
```

---

## Configuration

All environment variables are read in [ailienant-core/shared/config.py](ailienant-core/shared/config.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `LITELLM_PROXY_BASE_URL` | `http://localhost:4000` | LiteLLM proxy endpoint |
| `LITELLM_PROXY_API_KEY` | `sk-ailienant-local` | Proxy auth key |
| `AILIENANT_MODEL_SMALL` | tier-aliased | Cheap local model (e.g. `ollama/qwen2.5-coder:1.5b`) |
| `AILIENANT_MODEL_MEDIUM` | tier-aliased | Mid local model |
| `AILIENANT_MODEL_BIG` | tier-aliased | Cloud or heavy local model |
| `AILIENANT_MODEL_EMBEDDING` | OpenAI ada-002 alias | Vector embedder |
| `AILIENANT_MINI_JUDGE_MODEL` | small/cheap | Mini-Judge classifier |
| `AILIENANT_LANCEDB_PATH` | `ailienant_lancedb` | LanceDB store directory |
| `AILIENANT_CATALOG_DB` | `ailienant_catalog.sqlite` | SQLite catalog |
| `AILIENANT_PLANNER_DEBUG` | `1` | When `1`, planner returns a synthetic SDD without calling the LLM |
| `AILIENANT_MAX_BUDGET_USD` | (per task) | FinOps hard ceiling |
| `AILIENANT_EMBEDDING_DIM` | `1536` | Vector dimension |
| Cloud keys | unset | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `AILIENANT_CUSTOM_CLOUD_ENDPOINT` |

The four **intelligence profiles** the extension exposes:

| Profile | When to use it |
| --- | --- |
| **Medium** | Light local model only; fastest, lowest VRAM |
| **Big** | Heavy local model; refactors and multi-file work, larger VRAM footprint |
| **Cloud** | Forces cloud for every call; ignores routing math |
| **Hybrid** *(default)* | Lets the router pick per task via CSS × TCI × Mini-Judge |

---

## Core subsystems

### Hybrid routing (Phase 3.3)

CSS is computed as `(0.5 · semantic_similarity + 0.3 · graph_coverage + 0.2 · recency) × 100`. Red alert fires when `css < 40`. The Mini-Judge then runs a binary semantic-risk classifier:

- `HIGH` → veto to `CLOUD`, force `tci = 100`
- `MEDIUM` → escalate `LOCAL_SMALL` to `LOCAL_BIG`, clamp `tci ≥ 75`
- `NONE` → defer to the math (`tci < 30` → SMALL, `< 75` → BIG, ≥ 75 → CLOUD)

Source: [ailienant-core/agents/planner.py](ailienant-core/agents/planner.py), [ailienant-core/core/memory/context_auditor.py](ailienant-core/core/memory/context_auditor.py).

### GraphRAG retrieval (Phase 3.2)

`SemanticMemoryManager.search_with_paths` runs one embedding + cosine search and returns `(score, top_k_files)`. `GraphRAGDynamicExtractor.deep_parse` expands those seeds one hop via the SQLite `dependency_graph` table, reads each file through the VFS firewall, parses with Tree-sitter, and emits a `DeepParseResult` (target files, parsed files, formatted context block, coverage ratio, token count). Depth `k`, file cap, and token ceiling scale per tier (LOCAL_SMALL → k=1/10 files/4 K tokens; CLOUD → k=3/50 files/32 K tokens).

### Memory Janitor (Phase 3.5)

- **Vector GC.** Scans LanceDB `workspace_embeddings` filtered by `workspace_hash`, drops rows whose `file_path` no longer exists on disk. Sync work runs in `asyncio.to_thread`.
- **Graph purge.** Deletes `mcts_episodes` rows with `prune_reason IS NOT NULL AND accepted_at < ?` (default retention: 30 days).
- Triggered manually via `POST /api/v1/system/janitor`. Periodic daemon wiring is Phase 3.4.3b.

### Cognitive Fast-Boot (Phase 3.6)

`dump_state_to_markdown` writes a human-readable Markdown checkpoint with an embedded machine-JSON payload to `<workspace>/.ailienant/AGENTS.md` using a temp-file + `os.replace` atomic swap. `load_state_from_markdown` returns `None` if the file is missing or older than `max_age_seconds` (default 3600). The planner consults it before any LanceDB call; on a hit, the embedding step is skipped and only `deep_parse` runs.

### Hybrid MCTS Fixer + Circuit Breaker (Phase 3.4.8)

`local_fix_with_retry` runs `validate_delta` then up to 3 `Tier.LOCAL` repair calls, mutating `MCTSNode.error_streak`. When the streak hits `MAX_LOCAL_ATTEMPTS = 3`, `surgeon_escalation` invokes `Tier.CLOUD` and revalidates; the streak resets to 0 on success. `evaluate_node_reward` orchestrates the full sequence and short-circuits to `-1.0` if even the surgeon fails (no Supreme Judge call is wasted). Every LLM call is bucketed in the `token_ledger` (LOCAL vs CLOUD totals + an estimated `savings_usd`).

### Validation pipeline

- AST filter (Tree-sitter syntax check) — fast, language-agnostic.
- LSP filter (subprocess to `ruff`, `eslint`, etc.) — catches lints and undefined references.
- Virtual document overlay so the validation never touches disk.

Composed in [ailienant-core/tools/validation/pipeline.py](ailienant-core/tools/validation/pipeline.py).

### State management

`AIlienantGraphState` (see [ailienant-core/brain/state.py](ailienant-core/brain/state.py)) is a strictly typed `TypedDict` with custom reducers for parallel-fan-out keys (`vfs_buffer`, `generated_code`, `current_cost_usd`). The first planner turn freezes an `immutable_wbs`; the DriftMonitor compares every subsequent plan against that baseline and triggers a HITL escalation on divergence.

---

## Roadmap

The full roadmap lives in [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md). High-level snapshot:

| Phase | Status | Highlights |
| --- | --- | --- |
| 0 — Foundations | ✅ | Contracts, state, VFS middleware |
| 1 — Surface | ✅ | FastAPI, LLM gateway, model discovery, dirty-buffer capture |
| 2A–2D — Agent base | ✅ | Planner, CoderAgent stub, checkpoint WAL, FinOps gate, HITL ideation, Socratic analyst |
| 3.0 — Trajectory memory | ✅ | HNSW recall of past missions |
| 3.1 — LanceDB semantic search | ✅ | Multi-tenant `workspace_embeddings` |
| 3.2 — GraphRAG deep parse | ✅ | Tree-sitter + SQLite 1-hop expansion |
| 3.3 — Context Meter Cascade | ✅ | CSS, red alert, Mini-Judge veto |
| 3.4.x — MCTS foundation | ✅ | Tree, episodic audit, Mirror API, dual-rules resolver, hybrid local/cloud fixer |
| 3.5 — Memory Janitor | ✅ | Orphan-vector GC + obsolete graph purge |
| 3.6 — Cognitive Fast-Boot | ✅ | `.ailienant/AGENTS.md` atomic checkpoints |
| 3.7 — Checkpoint Gate | ✅ | Cross-subsystem E2E + stress suite (270 tests) |
| 3.4.3b — Daemon loop | ⏳ | Periodic janitor + fast-boot scheduler |
| 4 — Real CoderAgent | ⏳ | Tool-using LLM execution, MCP wiring |
| 5 — MCP ecosystem | ⏳ | External skill registry, RBAC enforcement |
| 6 — Auth + multi-user | ⏳ | Cloud deployment path |

---

## Testing and quality gates

The project enforces three gates on every change:

```powershell
# 1. Static typing on new / mutated core modules
.\venv\Scripts\mypy.exe core\janitor.py core\state_manager.py tests\test_phase3_checkpoint_gate.py `
  --strict --explicit-package-bases --follow-imports=silent

# 2. Lint
.\venv\Scripts\ruff.exe check .

# 3. Test suite (≥ 270 passing — zero regressions)
.\venv\Scripts\pytest.exe
```

Coverage is wide: VFS transactions, indexing, PPR centrality, drift monitoring, MCTS tree ops, Nightmare Protocol, MapReduce swarms, FinOps gate, rule distillation, hybrid routing, fast-boot, janitor, and the Phase 3.7 cross-subsystem gate (`tests/test_phase3_checkpoint_gate.py`).

---

## Design principles

1. **Local-first, cloud-when-it-helps.** The router defaults to local tiers. Cloud is reserved for high TCI, low CSS, or vetoed-MEDIUM/HIGH Mini-Judge verdicts. A token ledger quantifies the savings.
2. **Spec-Driven Development.** The PlannerAgent never executes code; it produces a `MissionSpecification`. The CoderAgent consumes it. Drift between replans triggers HITL.
3. **Fail fast, fail cheap.** Pydantic on every state mutation. Three local repair attempts before any cloud surgeon call. Circuit breakers everywhere a feedback loop can occur.
4. **Atomic writes.** Every disk artefact (rules, AGENTS.md, generated patches) uses `tempfile + os.replace`. No half-written files.
5. **Multi-tenant by default.** Every retrieval, every vector row, every GC predicate carries a SHA-256 workspace hash.
6. **Honest telemetry.** Token ledger separates LOCAL from CLOUD. Latency is measured, not estimated. Silent rejections (≥ 70 % overwrite within 3 min) are an explicit feedback signal, not a guess.
7. **Conservative defaults under load.** WAL mode, in-thread LanceDB calls, IO coalescing for parser bursts, hard size ceilings on file reads (500 KB), minification detection.

---

## Honest list of what is NOT implemented

The README that preceded this one promised a few things that are not yet in code. To save you a grep:

- **Wasm / gVisor sandboxing.** Validation today is in-process AST + LSP. `tools/agent_tools.py::run_command` is a stub for Phase 4.
- **Bento agent picker / agent launcher grid.** The sidebar has a master toggle and a profile selector only.
- **React Flow graph visualization, KPI dashboard, "Control Room" tab.** Not present in the extension.
- **MCP client.** `tools/mcp_adapter.py` defines the registry and the adapter shape, but `_call_mcp_tool` raises `NotImplementedError`. Scheduled for Phase 5.
- **Real CoderAgent code generation.** `agents/coder.py` and `agents/logic.py` reserve state slots and run the validation/patch pipeline; the actual tool-using LLM call is Phase 4.
- **MCTS search algorithm.** `brain/mcts/tree.py` defines `MCTSTree`, `MCTSNode`, UCB1 selection, and pruning. The full overnight search daemon (`brain/daemon.py`) is the Phase 3.4.3b deliverable.
- **Specialized agent classes** (`RefactorAgent`, `SecOpsAgent`, `InfraAgent`, `DebugAgent`, `TestAgent`, `DocAgent`). These are **roles** baked into `WBSStep.target_role`, not standalone files.
- **P2P agents, Enterprise tier, Supabase migration.** Aspirational future content; removed from this README to keep the document load-bearing. See `docs/PROJECT_MANIFEST.md` for any roadmap items that may revisit these.
- **RecencyBoost time decay.** `ContextMeter.recency_score` is a placeholder constant today; the Phase 3.7 gate intentionally tests AGENTS.md TTL but not decay (deferred to Phase 3.8+).

If you want a feature on this list, it is a great place to start contributing.

---

## Contributing

1. Read [CLAUDE.md](CLAUDE.md) — it documents the architectural guardrails and the protocol for raising conflicts before mutations.
2. Read [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) to find the current phase.
3. Open a draft PR with a one-line summary, a test plan, and a note on whether the change touches mypy-strict modules.
4. Every PR must keep `pytest`, `mypy --strict` (on the modules listed in the manifest), and `ruff` green.

When the orchestrator pairs with this codebase, it should:

- Run `pytest` and the relevant `mypy` invocation before declaring "done".
- Prefer editing existing files; create new ones only when the manifest says so.
- Never push to `main` without explicit approval; never rewrite git history.

---

## License

No license file is present yet. Until one is added, treat this repository as **source-available, all rights reserved**. A permissive licence is on the roadmap and will be added before the first public tag.

---

## Acknowledgments

AILIENANT stands on the shoulders of [LangGraph](https://github.com/langchain-ai/langgraph), [LanceDB](https://lancedb.com/), [Tree-sitter](https://tree-sitter.github.io/), [LiteLLM](https://github.com/BerriAI/litellm), [Pydantic](https://docs.pydantic.dev/), and the VS Code extensibility model. The orchestration patterns owe a debt to public writing on Claude Code, Cline, and Cursor.
