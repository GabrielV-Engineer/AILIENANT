# Phase 8.5 Blueprint — External Capability Gateway

## Objective

Turn AILIENANT from a **consumer** of external MCP tools into a **provider**: a multi-tool
**stdio MCP server** that lets external agents (Claude Code, Codex) drive AILIENANT, and lets the
benchmark be run and analyzed automatically. The gateway is a pure **adapter** over the existing
`/api/v1/task/submit` + WebSocket + token substrate — it adds no new orchestration and alters no
graph state. It rides what already ships.

This blueprint is the binding contract for sub-phases 8.5.1–8.5.7. It deepens the umbrella decision
(ADR-759) into executable locked decisions **D1–D8**; it does **not** mint new top-level ADR numbers
(the division is bound to a single ADR). The scope here is **backend-only** (Python,
`ailienant-core/`); the extension-side registration surface is a separate future concern, not
designed here. No production source is written in this docs sub-phase.

---

## Why (Architectural Rationale)

The permission foundation this track requires is now in place: the 8.4 hardening closed the
fail-open MCP classification (`classify_tool_privilege`), wired the dispatch guard
(`evaluate_action`), and shipped live HITL on real MCP tools. AILIENANT already **consumes** MCP
tools through that engine. The symmetric move — **serving** its own capabilities over MCP — is the
natural next step, and the benchmark (Division 8.3) becomes its first consumer rather than a
bespoke runner.

The hard problem is not "expose a tool." It is the **impedance mismatch** between two paradigms:

- An MCP tool call is **request/response, stdio JSON-RPC, no human in the loop**.
- AILIENANT's core value is a **long-running, streaming, HITL-gated** task.

A naive bridge hangs forever on the first approval prompt, or silently auto-applies dangerous
mutations, or leaks the host's full-fidelity interactive path to a caller that cannot use it. The
gateway resolves this by **degrading gracefully** (deny + structured report instead of hanging) and
keeping the REST+WS surface as the full-fidelity path for clients that *do* have a human — our own
extension.

**Reuse, not rebuild.** Every load-bearing asset already exists:

| Asset | Location | Role in 8.5 |
|---|---|---|
| Tool privilege classifier | `core/permissions.py` (`classify_tool_privilege`) | classifies the gateway's own capabilities — same precedence table |
| Permission evaluator | `core/permissions.py` (`evaluate_action`, `rbwe_guard`) | gates every gateway verb; **forking forbidden** |
| Task submit substrate | `main.py` (`POST /api/v1/task/submit`, 202, `TaskPayload`) | EXECUTE verbs ride this loopback |
| Token middleware | `main.py` (`_require_token`) | the loopback authenticates through it |
| HITL channel | `vfs_manager.request_human_approval` | deliberately **NOT** called for external callers (no human) |
| Out-of-band registry pattern | `tools/mcp_adapter.py` (`_sessions`, `_session_trust`) | the access shape for caller state (made durable here) |

---

## Locked Decisions (D1–D8)

### D1 — Two-path adapter, no second contract

EXECUTE verbs (`run_task`, `run_benchmark`) ride a **localhost loopback** to
`POST /api/v1/task/submit`, passing through the real `_require_token` and the exact public contract
— there is no parallel execution path to drift. READ_ONLY verbs (`query_memory`, `get_dependents`,
`get_workspace_graph`, `get_report`, `check_task_status`) call core managers directly in-process.
Module home (created in 8.5.1): `ailienant-core/gateway/`.

#### D1a — Host Discovery (lifecycle paradox)

The stdio gateway is a **separate ephemeral child process** spawned by the consuming agent
(`python -m ailienant_core.gateway`); it shares **no memory** with the FastAPI host that VS Code
started, so it cannot know the host port or loopback token a priori. Mechanism:

- On startup the FastAPI host writes an ephemeral state file `~/.ailienant/run.json` carrying
  `{ port, loopback_token, pid }`, best-effort removed on graceful shutdown.
- **Stale-file resilience.** The file is a **hint, not a truth** — a SIGKILL, crash, or power loss
  leaves it orphaned, pointing at a dead port. Before using it the gateway **verifies liveness**:
  the `pid` is alive **and** a strict **~2 s** loopback connection timeout succeeds. On dead-pid,
  refused, or timeout it treats the file as stale and returns a clean structured error to the
  external agent — *"AILIENANT host is not running. Please open VS Code to start the engine."* —
  never a raw `ConnectionRefusedError`.
- **Token-leak protection.** `run.json` carries the loopback service token, which is god-tier on the
  local API. The host MUST write it with strict **`0600`** owner-only permissions (the same posture
  as `core/config/mcp_secrets.py`), preventing cross-user theft on shared Linux/macOS hosts.
- **MVP is fail-fast.** Headless auto-start of the engine from the gateway is a deferred option;
  failing quickly with a clear message is safer for v1.

### D2 — Caller identity is a token-derived `caller_id`

Each external agent authenticates with its **own** gateway token; `caller_id` is derived from it.
This is distinct from the loopback service token (D1a) used to satisfy `_require_token`. The
`caller_id` is the key for all per-caller governance (D3).

### D3 — Budget/rate state is out-of-band **and durable**

A `GatewayCallerLedger` keyed by `caller_id` enforces a **token-bucket rate limiter** plus a
**cumulative token budget** — the DoS guard so one external agent cannot exhaust the host budget or
flood the task queue. Two binding constraints:

- **Durable, not merely in-memory.** The ledger is a **security control**. The stdio gateway is
  ephemeral, so an in-memory budget would reset to zero on every reconnect — an attacker or a
  looping agent defeats it by simply restarting the connection. The ledger therefore persists to
  disk (the existing local SQLite catalog DB, or a `filelock`-protected JSON co-located with it);
  consumed budget and rate state **survive a stdio restart**. The `_sessions`/`_session_trust`
  module-level pattern is the *access shape*, but persistence is mandatory here. **This lands in the
  8.5.2 MVP — it is not deferred.**
- **Out-of-band re the graph (immutability).** The ledger MUST NOT touch `AIlienantGraphState`,
  `ContextMeter`, or `MissionSpecification`, nor add a graph channel (per SCHEMA_EVOLUTION.MD). It
  lives entirely at the gateway edge.

### D4 — Conservative posture, fixed at the edge

External callers run under a **conservative permission posture** that the gateway pins **before**
`evaluate_action`; external input can never raise it (**no self-escalation**). Principle: **never
silent AUTO** (no free auto-apply of mutations or DANGEROUS actions) and HITL/DANGEROUS verdicts
degrade to deny (D5). The posture is built from the **existing** `SessionPermissionMode` /
`PermissionMode` enums — **do not mint a new mode** without a blueprint amendment. The precise
enum-or-composition that expresses "curated EXECUTE verbs may run, internal DANGEROUS is denied" is
finalized in 8.5.2 (see Conflict (g)).

### D5 — HITL-degrade: deny-report, never hang

When `evaluate_action` returns a HITL/DANGEROUS verdict, the gateway returns a **structured
deny-report** immediately — `{ status: "denied", tier, reason, would_have_required:
"human_approval" }` — and **never** calls `request_human_approval`. There is no human in an external
caller's loop, so a prompt would block a click that never comes. The REST+WS surface remains the
**full-fidelity** path (live streaming + interactive HITL) for clients that have a human — our own
extension.

### D6 — Async is a poll-pair, not HTTP 202 / stream

MCP-over-stdio is pure JSON-RPC: there is **no HTTP 202**, and an external LLM cannot consume a
"stream" from a tool-call response without native notification handling it rarely has. The async
contract is therefore a **two-tool poll pair** in the LLM tool-call paradigm:

- `run_task` / `run_benchmark` (EXECUTE) return the existing `task_id` plus a natural-language
  instruction — *"task in progress, poll its status with the returned id"*.
- `check_task_status` (READ_ONLY) takes a `task_id` and returns current status/result for the LLM to
  poll until completion.

The handle is the existing `task_id`; status lives in the existing task lifecycle — **no new state
store**. Short READ_ONLY verbs still return synchronously. `check_task_status` is a **blueprint
addition** to Capability Catalog v1 (it is the polling companion that ADR-759's summary table
omitted); it lands in 8.5.4.

### D7 — Versioning: semver public contract

The gateway surface is a **permanent public contract**. The server advertises a `protocol_version`
(semver) and a per-capability `schema_version` in `list_tools()`. A breaking change to a capability
schema bumps the major and announces deprecation under an **N-minor support window**; callers pin a
version.

### D8 — Symmetric permissions, no fork

The gateway's own capabilities are classified by `classify_tool_privilege` on the **same**
precedence table as any external tool, and their tiers feed `evaluate_action`. **Forking the
permission engine is forbidden** — consumer (8.4) and provider (8.5) share one engine.

---

## §3 Conflicts Raised (CLAUDE.md Architectural Foresight)

- **(a) Immutability vs. per-caller state.** Budget/rate is caller-scoped, mutable state; the graph
  schema is frozen. **Mitigation:** the out-of-band durable ledger (D3) — never a graph channel.
- **(b) READ_ONLY direct-call bypasses `_require_token`.** In-process READ_ONLY verbs skip the HTTP
  middleware. **Mitigation:** they MUST still run `classify_tool_privilege` + the caller ledger, so
  the direct path is not a privilege or DoS bypass.
- **(c) Loopback needs a valid token.** **Mitigation:** the host's `run.json` (D1a) supplies the
  ephemeral loopback service token; a user's UI token is never reused.
- **(d) HITL impedance.** External callers get a deliberately less-capable path (degrade-to-deny,
  D5). **Mitigation:** documented as intentional, not a gap; the full path stays REST+WS.
- **(e) Lifecycle gap.** The stdio gateway is a child process with no shared host memory; absent a
  running host it MUST fail-fast with a clean "open VS Code" error (D1a), never a raw exception.
- **(f) Ledger amnesia.** An ephemeral process makes an in-memory budget a no-op against reconnect;
  the ledger MUST be disk-durable (D3).
- **(g) No existing `SessionPermissionMode` cleanly expresses the conservative posture.** `PLAN`
  blocks EXECUTE (kills `run_task`), `DEFAULT` HITLs EXECUTE (→ degrade-to-deny, also kills it),
  `AUTO` silently applies mutations (violates "never silent AUTO"). **Resolution:** 8.5.2 composes
  the posture from the existing enums — curated gateway EXECUTE verbs are pre-authorized at the
  capability layer (catalog tier + caller policy), while the spawned task's internal tool calls are
  pinned so DANGEROUS/HITL degrades to deny (D5). No new enum without an amendment (D4).

---

## WBS — Subfases (umbrella ADR-759)

Detailed per-subfase scope and DoD live in the manifest WBS (`PROJECT_MANIFEST.md → División 8.5`).
This blueprint is the binding contract; any deviation requires a blueprint amendment in the same PR.

| Sub | Title | Realizes | Net-new |
|---|---|---|---|
| 8.5.1 | Gateway framework | D1, D1a, D6, D7 | stdio MCP server, `list_tools()`, per-tool JSON schema, host-discovery `run.json` read + host-side writer hook, async poll-pair |
| 8.5.2 | Tier governance | D2, D3, D4, D8 | symmetric classification, conservative posture, token-derived `caller_id`, **disk-durable** per-caller budget + rate ceiling |
| 8.5.3 | HITL-degrade | D5 | DANGEROUS/HITL → structured deny-report, never hangs |
| 8.5.4 | Capability Catalog v1 | D6 | `run_task` (EXECUTE, conservative) + `check_task_status`/`query_memory`/`get_dependents`/`get_workspace_graph` (READ_ONLY) |
| 8.5.5 | Eval surface tools | D1, D6 | `run_benchmark` (EXECUTE, budget-gated, async) + `get_report` (READ_ONLY) over the 8.3.5 `report.json` |
| 8.5.6 | Versioning + auth ergonomics + integration docs | D7 | semver + deprecation policy, token ergonomics, per-caller ceiling docs |
| 8.5.7 | DoD-check | all | external caller lists catalog → runs READ_ONLY verb → DANGEROUS verb denied+reported without hanging |

### Capability Catalog v1

| Capability | Tier | Notes |
|---|---|---|
| `run_task` | EXECUTE (conservative) | wraps submit+WS loopback; async poll-pair; HITL-degrades |
| `check_task_status` | READ_ONLY | **blueprint addition** — polling companion for the async verbs |
| `query_memory` | READ_ONLY | GraphRAG / memory query |
| `get_dependents` | READ_ONLY | dependency graph, 1-hop backward |
| `get_workspace_graph` | READ_ONLY | code-graph snapshot |
| `run_benchmark` | EXECUTE (budget-gated, async) | runs the Division 8.3 harness |
| `get_report` | READ_ONLY | reads the 8.3.5 `report.json` |

**The benchmark is the first consumer of the gateway, not its design goal** —
`run_benchmark`/`get_report` are two members of a general capability surface.

---

## Staging (MVP → Enterprise, CLAUDE.md §7)

- **MVP = 8.5.1–8.5.4** — framework + host discovery (D1a), tier governance + **disk-durable ledger
  (D3)**, HITL-degrade, and the READ-heavy catalog with a conservative `run_task` + `check_task_status`.
- **8.5.5** is gated on the 8.3.5 `report.json` contract.
- **8.5.6** = versioning + integration docs.
- **Ledger persistence is part of the MVP (8.5.2), NOT deferred** — it is a security control.
- **Enterprise-deferred** (log to `TECH_DEBT_BACKLOG.md` if adopted): the extension-side registration
  surface; headless host auto-start from the gateway. No tactical MVP shortcut is taken in the
  in-scope sub-phases; any contained patch is declared in its PR with a deferred refactor row.

---

## 🔒 LOCK

**Every EXECUTE-tier gateway tool is hard-blocked by 8.4.1.** Shipping an EXECUTE verb over the
fail-open classification would reopen DEBT-026 — the exact hole this lineage closed. 8.4.1 is closed,
so the lock is **satisfied**; EXECUTE verbs may proceed. READ_ONLY verbs are unconditionally fine.
Same gate pattern as the 7.14.7 ↔ 7.15.7 LOCK.

---

## Verification

This sub-phase (8.5.0) is **docs-only**; its gate is documentary — the blueprint exists and is
coherent, the manifest row is checked, the journal and README are updated, and no code/config is
mutated.

The **code** sub-phases (8.5.1+) inherit the standard gate:

```powershell
cd C:\Proyectos\Proyect_Ailienant\ailienant-core
.\venv\Scripts\python -m mypy .                 # enforced gate, stays 0
.\venv\Scripts\python -m pytest -q              # full suite, regression-free

cd ..\ailienant-extension
npm run compile                                 # tsc --noEmit + eslint + esbuild → 0
```

The division closes at the Phase 8 checkpoint gate (Division 8.6), which re-certifies fail-closed MCP
privilege and external HITL-degrade among its rows.
