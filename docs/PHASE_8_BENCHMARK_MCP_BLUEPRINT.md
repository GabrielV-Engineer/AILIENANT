# Phase 8 Blueprint — Precision Benchmarking, MCP Ecosystem & External Capability Gateway

> Master architectural contract for Phase 8 Divisions **8.3 / 8.4 / 8.5 / 8.6**. Binding while any of those divisions is active. This is the **dedicated** Phase-8 blueprint for the benchmark/MCP/gateway track; the type-strictness campaign (Divisions 8.0/8.1) lives in `PHASE_8_BLUEPRINT.md` and is intentionally kept separate.
>
> Each ADR below is self-contained: an implementer can build its division from this file alone, with no further design.

## Status

| ADR | Division | Title | State |
|-----|----------|-------|-------|
| ADR-756 | 8.3 | Precision Benchmarking & Ablation Methodology | OPEN |
| ADR-757 | 8.4 | MCP Config SSoT + Tool Privilege Classification | PARTIALLY CLOSED (8.4.0–8.4.4 ✅; 8.4.5–8.4.7 OPEN) |
| ADR-758 | 8.5 | Ailienant-as-MCP-Server (origin) | SUPERSEDED by ADR-759 |
| ADR-759 | 8.5 | External Capability Gateway | OPEN |
| ADR-760 | 8.6 | Phase 8 Checkpoint Gate | OPEN |

---

## ADR-756 — Precision Benchmarking & Ablation Methodology (Division 8.3)

### Goal

Prove, empirically and defensibly, two things: (1) using a model **through Ailienant** raises its code-task accuracy vs using the model alone; (2) the **TCI-based tier routing** (small/big/cloud) retains accuracy while cutting token cost. The output is an auditable `report.json` and a resilience/precision report at the phase gate.

### Two metrics — explicitly distinct (never both called "Pass@k")

- **Pass@1** (plain code generation, 8.3.1): canonical single-shot correctness of one generation against the problem's unit tests. The literature's `Pass@k` draws **k independent samples** and counts success if any passes — but at `temperature=0` those k samples are identical, so `Pass@k` degenerates to `Pass@1`. Plain-codegen therefore reports **Pass@1 only**.
- **Resolve@k** (multi-file refactor, 8.3.2): resolved within **k≤3 dependent self-correction cycles** of the ReAct loop (run → structured verdict → edit → re-run). This is a fundamentally different quantity from independent-sample Pass@k — the cycles are **dependent** (each consumes the previous cycle's error feedback) — and it is **valid at `temp=0`** precisely because each cycle's context changes. **In arms with no ReAct loop (G1 zero-shot, G3 ReAct-off), Resolve@3 collapses to Resolve@1** (a single attempt; the loop never iterates).

### Hypotheses — a 2×2 factorial (model × pipeline / pipeline × routing)

The confound to avoid: if the router sends hard problems to a stronger model, an observed uplift would be attributable to **model size**, not to the architecture. The clean design fixes one axis per hypothesis.

- **H₁ — precision uplift (architecture; model held constant).** On multi-file refactors restricted to **TCI > 60**:

  `Resolve@3(G4) ≥ 1.25 × Resolve@3(G1)`

  with **G1 and G4 both pinned to the same tier** (CLOUD for TCI>60), so the uplift is attributable to GraphRAG + ReAct, not to a stronger model. **Routing is H₂'s variable, never H₁'s.**

- **H₂ — cost efficiency (routing; pipeline held constant).**

  `Resolve@3(TCI-routing) ≥ 0.95 × Resolve@3(G4-force-cloud)`  **and**  `tokens(TCI-routing) ≤ 0.60 × tokens(G4-force-cloud)`

  **G4-force-cloud** is a named **fifth experimental arm**: G4's full pipeline with a **harness-level override** of `resolve_provider`'s output forced to CLOUD (NOT production code — a test seam). It is the baseline H₂ is falsified against.

The two hypotheses are **measured independently** — one may hold while the other fails. Stating them multiplicatively removes the relative-vs-percentage-points ambiguity ("≥25%" means `× 1.25`, not `+25 pp`).

### Ablation matrix (Setup A)

| Arm | Retrieval | Routing | ReAct loop | Isolates |
|-----|-----------|---------|------------|----------|
| **G1** Control | none (zero-shot) | n/a (fixed tier) | off | base model |
| **G2** RAG-only | **vector-only (net-new)** | n/a | off | flat retrieval value |
| **G3** Ailienant-Core | GraphRAG | TCI | **off** (`requires_iteration=False`) | graph value (vs G2) |
| **G4** Ailienant-Full | GraphRAG | TCI | on (swarm + self-heal) | ReAct value (vs G3) |
| **G4-force-cloud** | GraphRAG | **forced CLOUD** | on | routing value (H₂ baseline) |

Decomposition: **G2↔G3 isolates the graph**; **G3↔G4 isolates ReAct**; **G4↔G4-force-cloud isolates routing**.

**G2 is net-new code (verified):** no graph-disable flag exists in the codebase — the system is GraphRAG-native. The vector-only retriever is implemented as a **Strategy injection living in `tests/benchmark/`** (an implementation of the retrieval interface), **NOT** a `if benchmark_mode:` flag inside `core/memory/graphrag_extractor.py`. The production hot path stays clean; the degraded retriever exists only for the benchmark.

**G3 toggle (verified present):** force `WBSStep.requires_iteration = False` (the gate the planner/engine already read to route a step into the ReAct cell).

### Statistics

- **`n` counts distinct problems per group** (a binomial proportion), **not** reruns of one problem. n≥30 distinct problems per group.
- Report each proportion with a **Wilson confidence interval** (correct for proportions near 0/1 and small n; the normal approximation is not).
- **Reruns are reserved** for the few cloud cases that show observable run-to-run variance — not applied uniformly (at `temp=0` most problems are stable, so 30 reruns/problem would be waste).
- `seed=42`, `temperature=0`. **Caveat (declared, not hidden):** `temp=0` does **not** guarantee bitwise determinism on cloud or quantized models under load — which is exactly why the design relies on n + confidence intervals, not on determinism-as-sold.

### Token Efficiency Ratio

`tokens_consumed / problems_resolved`, **stratified by TCI bucket** `[0,40) / [40,75) / [75,100]`. The raw (unstratified) ratio has a composition trap: a system that solves the cheap easies and fails the expensive hards shows a flattering ratio while being useless. Stratification exposes that.

### Cost ceiling

The harness enforces a **`benchmark_budget_usd`** cap, separate from the runtime FinOps governor. Back-of-envelope: `n_problems × 5 arms (incl. force-cloud) × ≤3 cycles × per-cycle cloud cost`. A run that hits the cap **aborts cleanly** (partial `report.json` flagged incomplete) rather than silently burning budget — a PM-grade guard against an unbounded ablation sweep on a local-first project.

### Frozen corpus + BenchmarkOracle

- **Frozen corpus (non-negotiable for reproducibility):** the custom multi-file benchmark uses a **pinned-commit snapshot** of the codebase + **golden patches** + that snapshot's test suite — **never the live codebase** (a moving target makes two runs on different dates measure different things, breaking `seed=42`/n≥30). The corpus fixture records the exact git SHA.
- **BenchmarkOracle contract:** `run_oracle(workspace_snapshot, applied_patch) -> Verdict{passed: bool, failures: [...]}` — applies the candidate patch over the frozen snapshot, runs the snapshot's test suite in the sandbox, and returns pass/fail. Its verdict is the **sole arbiter** of Resolve@k. Without a defined oracle the report is not defensible.

### Indexing & cache hygiene (mandatory — GraphRAG is only as good as its index)

GraphRAG's value is the entire point of the G2↔G3 delta, and it depends on a **fully built index** of the frozen corpus. Four requirements, all non-negotiable:

1. **Pre-index, await completion.** Before the first problem runs, index the frozen snapshot via `LazyIndexer` ([core/indexer.py](../ailienant-core/core/indexer.py)) and **block until `is_complete`**. The indexer is background + low-priority + lazy by design — a problem that runs against a half-built graph sees `get_dependents() == []`, `coverage_ratio == 0`, a collapsed CSS, and G3/G4 lose exactly the advantage under measurement. A correctness assert (`get_dependents(seed)` non-empty over the indexed corpus) gates the run.
2. **Index reuse, timed separately.** The index is built **once per corpus** and reused across all n problems and across every GraphRAG-using arm (G3, G4, G4-force-cloud) — never rebuilt per problem. Indexing wall-clock is a **one-time cost** recorded as `indexing_time_s` in `report.json`, **excluded** from per-problem latency.
3. **Embedding pre-flight.** The semantic layer (and therefore CSS `sem_score`) requires a live embedding backend (`MODEL_EMBEDDING`). The `LazyIndexer` pre-flight already aborts if it is unreachable; the harness surfaces that as a hard run-abort with a clear message, never a silent zero-retrieval run.
4. **Response cache OFF.** The semantic `response_cache` (7.18.4) must be disabled for the whole benchmark. A cache hit serves a prior answer for free, which **falsifies token accounting and the efficiency ratio** — H₂ would be measuring cache locality, not routing. A test asserts a repeated problem recomputes (non-zero token delta).

`routing_decisions` telemetry is the source of truth for per-problem TCI/CSS used in bucket stratification — the harness reads it back rather than recomputing.

### Execution model (hybrid)

- **Ablation (8.3.1–8.3.4):** in-process — calls `process_task()` directly (fast, deterministic, n≥30 feasible).
- **E2E gate (8.2.1 / 8.6):** 2–3 cases over the **real HTTP/WS surface** (`/api/v1/task/submit` + WS) to certify transport.

### `report.json` (8.3.5) — machine-readable, gateway-consumable

Contains: per-test `Verdict` rows · per-group aggregates · Wilson CI per proportion · H₁/H₂ verdicts (pass/fail against the multiplicative thresholds) · TCI-bucket-stratified Token Efficiency Ratio · ablation deltas (G2↔G3, G3↔G4, G4↔force-cloud). Consumed by the gateway eval surface (`get_report`, 8.5.5).

### Reused primitives (build nothing new here)

TCI ([brain/state.py](../ailienant-core/brain/state.py)), `resolve_provider`/`derive_routing_decision` ([brain/routing_engine.py](../ailienant-core/brain/routing_engine.py)), `get_dependents` ([core/db.py](../ailienant-core/core/db.py)) + `bfs_k_hop_backward` ([core/memory/graphrag_extractor.py](../ailienant-core/core/memory/graphrag_extractor.py)), `token_ledger`, `process_task` ([core/task_service.py](../ailienant-core/core/task_service.py)), AST validation ([tools/validation/ast_filter.py](../ailienant-core/tools/validation/ast_filter.py)). **Net-new:** harness orchestration, the G2 vector-only Strategy, the BenchmarkOracle, the report generator.

---

## ADR-757 — MCP Config SSoT + Tool Privilege Classification (Division 8.4)

### Config SSoT — Repository pattern, not a second source of truth

The **SQLite registry** (`mcp_servers` table, [core/db.py](../ailienant-core/core/db.py), with REST CRUD `/api/v1/mcp/*` + VS Code UI) **remains the runtime SSoT**. `.ailienant/config.json` is an **added serializable projection** of that truth — portability + git-committable parity with Cursor/Claude Code — not a relocation of the truth.

- **Import = idempotent upsert keyed by server name.** Loading `.ailienant/config.json` reconciles into SQLite without duplicating or clobbering existing rows.
- **Secrets NEVER enter the JSON.** The JSON carries `key_ref: "vscode_secret:<id>"`; the secret value lives in **VS Code SecretStorage**. Without this, the first `git push` leaks credentials.
- **Export edge:** a server whose secret lives only in SQLite exports a `key_ref` **placeholder**. Importing on a fresh machine **prompts for the secret** — it does not travel, there is no secret round-trip.

### Tool privilege classification — fail-closed

The live bug (DEBT-026): [mcp_adapter.py:344](../ailienant-core/tools/mcp_adapter.py) hardcodes every discovered tool to `READ_ONLY` — **fail-open** at a security boundary. The fix replaces that hardcode with:

`classify_tool_privilege(tool_name, description, server_name) -> ToolPrivilegeTier`

**Precedence (highest authority first): curated catalog > verb heuristic > DANGEROUS default.**

**Verb heuristic** (matched against tool name/description, lowercased):

| Pattern | Tier |
|---------|------|
| `get` / `list` / `read` / `search` / `fetch` / `describe` | READ_ONLY |
| `create` / `update` / `write` / `push` / `add` / `set` | WRITE |
| `exec` / `run` / `invoke` / `spawn` | EXECUTE |
| `delete` / `drop` / `force` / `merge` / `reset` / `purge` | DANGEROUS |
| **unmatched** | **DANGEROUS** (fail-closed) |

**Curated catalog overrides** (correct the heuristic's blind spots for the regulated servers — 8.4.2):
- `brave-search.search` → **READ_ONLY** (the heuristic's "search" already lands here, but the catalog pins it).
- `github.merge_pull_request` → **DANGEROUS** (the heuristic would read "merge" as DANGEROUS — confirmed; the catalog makes it explicit and authoritative).
- `github.create_pull_request` → **WRITE**; `docker.run` → **EXECUTE**; `postgres.query` → READ_ONLY, `postgres.execute` → EXECUTE.

The result feeds the **existing** `evaluate_action`/`rbwe_guard`/`gate_execute_action` ([core/permissions.py](../ailienant-core/core/permissions.py)) — so the Asymmetric Friction HITL finally fires on a mutating external tool.

### UX relief valve — or fail-closed becomes unusable

A `DANGEROUS` default means a brand-new server full of read-only tools would spam HITL on every call until catalogued → alarm fatigue → the user disables the whole protocol (the worst security outcome). Mitigation: a **session-scoped "trust this tool / elevate-once" memo**. The default stays safe; the human can consciously relax a specific tool for the session, never by omission.

### Amendment — secret substrate is backend-mask, not VS Code SecretStorage

The original wording placed the secret value in **VS Code SecretStorage**, referenced from `config.json` by `key_ref: "vscode_secret:<id>"`. The codebase has **no SecretStorage** anywhere, and the configuration surface (the dashboard) is an HTTP-served `fetch`-only webview that cannot reach the SecretStorage API (host-only). The established secret-bearing feature (BYOM) instead stores credentials backend-side (`byom_config.json`, `0600`) and masks them on read.

**Amendment:** the `key_ref` placeholder convention is retained verbatim in `config.json` (a secret value never enters the JSON), but the **value substrate is the codebase-consistent backend-mask pattern**, not SecretStorage. Additionally, `export` **defensively redacts URL-userinfo credentials** from a server's `uri` (e.g. a `postgresql://user:pass@host/db` connection string a user typed inline), so a credential cannot leak through the uri field even before a structured secret store exists. The secret-value store + connect-time env injection is deferred (DEBT-031).

### Amendment — multi-session registry + dispatch gate (8.4.4, closes DEBT-027)

The original ADR assumed a **single** `_session_singleton`. The production requirement (multiple `enabled` servers in the catalog) demanded a registry keyed by `server_name`:

```python
_sessions: Dict[str, ClientSession]
_exit_stacks: Dict[str, AsyncExitStack]   # one stack per server, never shared
```

**Key decisions (binding for 8.4.5+):**

1. **Idempotent bootstrap** — `if key in _sessions: return True` before any I/O. Re-invocation never spawns a duplicate stdio process.
2. **Per-server exit stacks** — one `AsyncExitStack` per connected server stored in `_exit_stacks`. A single server's failure or reconnect cannot entangle another server's stdio process.
3. **`shutdown_mcp_sessions()`** — single explicit teardown choke point, wired into FastAPI `lifespan` shutdown block. No background finalizer.
4. **Auto-connect in FastAPI lifespan startup** (after `init_registry()`), not per-task. MCP sessions are server-lifecycle resources, not request-scoped. A lazy `if not _sessions:` guard in `task_service.py` covers cold-start when startup connected nothing — no per-task DB cost in steady state.
5. **`evaluate_action` dispatch gate in `_arun`** — injected-approval callable (never `from api/`); gate activates only when the caller passes `session_permission_mode` (the "contract" pattern, mirrors `SandboxBashTool`). READ_ONLY short-circuits to ALLOW before the floor is consulted. DANGEROUS overrides AUTO. Verdict: DENY / HITL / ALLOW.
6. **`request_kind="MCP_TOOL_CALL"`** — free `Optional[str]` (no enum); toast bridge falls back gracefully for unknown kinds. FE HITL card severity/title binding is 8.4.7.
7. **Env-configurable HITL timeout** — `MCP_HITL_TIMEOUT_SEC` (default 120 s).
8. **Catalog overrides now bind live** — `server_name` is threaded into `classify_tool_privilege` at harvest, activating the qualified-key path (`postgres.query` → READ_ONLY) for the first time.

**DEBT-027 closed.** Deferred to 8.4.7: trust-once session-scoped valve (DEBT-029 remainder), live e2e graph-cell dispatch, FE HITL-card binding for `MCP_TOOL_CALL` kind.

---

## ADR-758 — Ailienant-as-MCP-Server (origin) — SUPERSEDED by ADR-759

The seed decision: a thin MCP **server** (stdio) exposing a single `ailienant.run_task` tool over the existing `/api/v1/task/submit` + WS + token substrate, with a conservative-permission default and HITL-degrade. Retained as the historical single-tool origin; **generalized by ADR-759**. No implementation derives from this ADR directly — build from ADR-759.

---

## ADR-759 — External Capability Gateway (Division 8.5, generalizes ADR-758)

### What it is

A **multi-tool stdio MCP server** in `ailienant-core/` Python that lets external agents (Claude Code, Codex) drive Ailienant — and lets the benchmark be run/analyzed automatically. It is an **adapter** over the existing `/api/v1/task/submit` + WS + `_require_token` substrate; the registry/install UI half is `ailienant-extension/` TS. (No source is written in the docs task — these are the eventual implementation domains.)

### Immutability (per SCHEMA_EVOLUTION.MD)

The gateway **MUST NOT** alter `AIlienantGraphState`, `ContextMeter`, or `MissionSpecification`, nor add a graph channel. It rides the existing submit+WS contract as a pure adapter. Any temptation to thread gateway-specific state through the graph is out of bounds — the gateway translates at the edge.

### Symmetric permission model (consumer == provider)

Ailienant consumes external MCP tools (Division 8.4) **and** serves its own over MCP. The gateway's own exposed tools route through the **same** `classify_tool_privilege` / `evaluate_action` / `rbwe_guard` engine. **Forking the permission engine is forbidden.** A capability is classified by the same precedence table as any external tool.

### Conservative-permission mode + DoS guard

- External callers run in a **conservative permission mode**: never silent AUTO, and an external caller **cannot self-escalate** to auto-apply.
- **Per-caller budget + rate ceiling** — a DoS guard so one external agent cannot exhaust the host's token budget or flood the task queue.

### HITL-degrade (the impedance mismatch)

A tool call is request/response; Ailienant's core value is a **long-running, streaming, HITL-gated** task. There is no human in an external caller's loop. Therefore: a tier-`DANGEROUS`/`HITL` action **degrades to deny + a structured report** (what was blocked and why), and **never hangs** waiting for a click that will never come. The REST+WS surface remains the **full-fidelity path** (streaming + interactive HITL) for clients that *do* have a human — our own extension.

### Async protocol

Long-running verbs use **202 + poll/stream** (mirrors the HTTP surface): `run_task`/`run_benchmark` return a handle immediately; the caller polls or streams progress. Short READ_ONLY verbs return synchronously.

### Versioning

The gateway surface is a **permanent public contract** → **semver + a deprecation policy**. A breaking change to a capability schema bumps the major and announces deprecation; callers pin a version.

### Capability Catalog v1 (starter, READ-heavy)

| Capability | Tier | Notes |
|------------|------|-------|
| `run_task` | EXECUTE (conservative) | wraps submit+WS; async 202+stream; HITL-degrades |
| `query_memory` | READ_ONLY | GraphRAG/memory query |
| `get_dependents` | READ_ONLY | dependency graph 1-hop backward |
| `get_workspace_graph` | READ_ONLY | code-graph snapshot |
| `run_benchmark` | EXECUTE (budget-gated, async) | runs Division 8.3 harness |
| `get_report` | READ_ONLY | reads 8.3.5 `report.json` |

**The benchmark is the first consumer of the gateway, not its design goal** — `run_benchmark`/`get_report` are two members of a general capability surface.

### 🔒 LOCK

**Every EXECUTE-tier gateway tool is hard-blocked by 8.4.1.** Shipping any EXECUTE verb over the fail-open classification reopens DEBT-026 — the exact hole this track closes. READ_ONLY verbs may proceed before 8.4.1 lands. Same gate pattern as the 7.14.7↔7.15.7 LOCK.

---

## ADR-760 — Phase 8 Checkpoint Gate (Division 8.6)

The single phase gate (sibling-file convention, like `test_phase7_13_checkpoint_gate.py` / `test_phase7_19_checkpoint_gate.py`): one load-bearing assertion per row, invoking real entry points, modifying no logic. Re-certifies: resilience (8.2 fallbacks fire), precision (the H₁/H₂ harness runs and emits a schema-valid `report.json`), MCP privilege **fail-closed** (a mutating tool never lands READ_ONLY), and external **HITL-degrade** (a DANGEROUS gateway verb returns deny-report, never hangs). **DoD:** `pytest` green + `mypy .` 0 + gate green + `npm run compile` 0. The Phase 8 benchmark/MCP/gateway LOCK set expires when this row is `[x]`.
