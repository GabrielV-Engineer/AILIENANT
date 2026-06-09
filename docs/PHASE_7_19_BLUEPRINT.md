# Phase 7.19 Blueprint — Agentic Execution Cell & Persistent Audit Trail

## Objective

Elevate AILIENANT's feedback loop from a **structured, batch, planner-mediated** cycle (Phase 7.18.0)
to a **continuous, LLM-driven ReAct loop** with a **bidirectional persistent terminal** — Claude-Code /
Codex parity — **without discarding** the LangGraph orchestration + MCTS reasoning that already exists.
The agentic cell is introduced as a bounded sub-loop *inside* the graph, not as a replacement for it.

---

## Why (Architectural Rationale)

7.18.0 wired a closed loop, but it is **batch and graph-driven**:

1. The **planner** must emit a `run_command` WBS step (command overloaded into `target_file`).
2. The coder dispatches **one** command via `get_active_adapter().execute()` — output buffered whole,
   `tty=False`, no `stdin`, no streaming, no interrupt.
3. The graph retries by edges (`route_after_coder → error_correction`) up to `CORRECTION_MAX_ATTEMPTS`.

`validate_output` only validates the **output schema** — it does **not** run tests. The terminal is a
*planned step*, not a surface the LLM drives. What distinguishes an autonomous engineer is the
**agentic loop**: the LLM decides `run pytest → read streamed output → reason → edit → run again` in a
free ReAct cycle over a **live terminal**.

**Glass-Box paradigm:** a 3-minute opaque spinner while the agent runs `npm install` or compiles
binaries erodes trust. We stream granular WebSocket events (`tool_call_start`, `pty_chunk`,
`ast_mutation_diff`) so execution is observable, not a black box.

**Reuse, not rebuild.** Already shipped and reused here:

| Asset | Location | Role in 7.19 |
|---|---|---|
| 3-tier sandbox + resolver | `core/sandbox.py` | extended with `SandboxSession` (streaming/stdin) |
| Execute-tier dispatch | `tools/execution_tools.py` | session-aware dispatch |
| DANGEROUS interceptor | `tools/execution_tools.py` | pre-spawn choke-point (unchanged) |
| Permission gates | `core/permissions.py` | session-level gating |
| Structured diagnostics | `tools/validation/diagnostics.py` | each iteration's reward signal |
| Host streaming render | Phase 7.16/7.17 (shiki + rAF-coalesce) | terminal/audit UI |
| Self-heal machinery | `error_correction`, breaker, budgets | governor base |
| MCTS | `brain/mcts/`, `agents/mcts_coder.py` | candidate selection (closes DEBT-009) |

---

## Locked Decisions (IT Director / Systems Architect)

1. **Native Direct governance = hybrid.** Strict allowlist for standard tooling (frictionless happy
   path) + **session-level approval** for unknown commands (enterprise security for dangerous actions).
   NOT per-command HITL (that would make an agentic loop unusable).
2. **The agentic cell coexists with `run_command`.** A router sends trivial single-step ops through the
   simpler graph path (latency/token optimization); complex multi-step debugging through the ReAct cell.
3. **Bidirectional PTY from Day 1 (not deferred).** A process hung on `[Y/n]` with unidirectional
   `subprocess.run` would block FastAPI's event loop. A persistent PTY with an open stdin pipe lets the
   user *or* the LLM inject input/interrupt, preserving async loop health.

---

## §3 Conflicts Raised (CLAUDE.md Architectural Foresight)

- **(a) Determinism vs. Rewind (Phase 7.13).** A free agentic loop is less deterministic than fixed
  nodes. **Mitigation:** every cell iteration MUST emit a checkpoint + trajectory record, so Rewind and
  trajectory memory stay intact.
- **(b) Semantic cache (Phase 7.18.4) assumes single-shot coder.** A multi-turn loop interacts
  differently with the AST-hash cache. **Mitigation:** the cell sets `bypass_cache` per iteration; the
  cache continues to serve the single-shot planner/coder paths.
- **(c) MCTS-into-live-loop (DEBT-009).** Deferred in 7.18.5 because its reward signal (the structured
  verdict) did not yet exist. It now does → the cell is its natural home; DEBT-009 closes here.

---

## WBS — Subfases (ADRs 747–755)

| Sub | Title | ADR | Net-new |
|---|---|---|---|
| 7.19.0 | `SandboxSession` Contract & PTY Backend Multiplexer | 747 | session ABC + Docker-stream + Native-Direct PTY |
| 7.19.1 | Workspace Synchronization Engine (VFS ↔ Sandbox · OCC) | 748 | AST-aware bidi sync, OCC guard |
| 7.19.2 | Agentic Execution Cell (ReAct sub-loop) | 749 | LangGraph node + 3 tools, coexists w/ run_command |
| 7.19.3 | Multi-Axis Iteration Governor (circuit breaker) | 750 | steps ∧ tokens ∧ time bounds |
| 7.19.4 | WebSocket Telemetry API & Event Dispatcher | 751 | typed granular deltas, O(1) routing, GC |
| 7.19.5 | Frontend: Shadow-DOM Audit Widgets | 752 | collapsible accordions, virtualized >1000 lines |
| 7.19.6 | Frontend: Interactive Chat PTY (xterm.js) + Composer Send/Stop Toggle | 753 | live stream + stdin injection + send-button mutates to stop (same button) |
| 7.19.7 | Structured Agent Output: Execution Checklist & Rich Explanatory Rendering | 754 | progressive ☐→✅ checklist (reuses emit_graph_mutation) + cross-mode WBS-seed + scenario-driven GFM tables/lists |
| 7.19.8 | Checkpoint Gate Phase 7.19 | 755 | sibling test-only gate |

Detailed per-subfase scope, DoD, and risk notes live in the manifest WBS
(`PROJECT_MANIFEST.md → FASE 7.19`). This blueprint is the binding contract; any deviation requires a
blueprint amendment in the same PR.

---

## Cost Function (FinOps bound for the Governor)

For a task running `N` ReAct iterations:

```
Cost_total = Σ_{i=1..N} ( C_in · T_in^(i) + C_out · T_out^(i) )
```

`N` is bounded by the multi-axis governor (7.19.3): `N ≤ N_max`, `Σ tokens ≤ token_cap`,
`elapsed ≤ time_cap`. Any axis tripping concedes gracefully (no infinite loop).

---

## Staging (MVP → Enterprise, CLAUDE.md §7)

- **7.19.0–7.19.4** = the full functional loop (backend): persistent streaming terminal, ReAct cell,
  governor, Glass-Box telemetry.
- **7.19.5–7.19.6** = the operator-facing surface (audit widgets + interactive PTY).
- No tactical MVP shortcut is taken; if any sub-phase must ship a contained patch, it is declared in its
  PR and a deferred Enterprise-Refactor row is logged in `TECH_DEBT_BACKLOG.md`.

---

## Verification (per sub-phase)

```powershell
cd C:\Proyectos\Proyect_Ailienant\ailienant-core
.\venv\Scripts\python -m mypy .                 # enforced gate, stays 0
.\venv\Scripts\python -m pytest -q              # full suite, regression-free

cd ..\ailienant-extension
npm run compile                                 # tsc --noEmit + eslint + esbuild → 0
```

Phase closes when `tests/test_phase7_19_checkpoint_gate.py` is green (7.19.8).
