# Phase 8 — Strict Typing Eradication WBS

## Objective

`mypy --strict main.py` → exit 0. Zero suppressed modules in `mypy.ini`.

The project's **enforced gate** (`mypy .` with per-module `follow_imports = silent` config) stays green
throughout the campaign. Each subfase adds a *new* strict gate atop it — never breaking the existing
one.

---

## Current State Baseline (2026-06-05 — re-baselined after Phase 8.0.0 sweep)

| Gate | Result |
|---|---|
| `mypy .` | ✅ Success — 247 files, 0 errors |
| `mypy --strict main.py` | ❌ 1 error in 1 file (`swarms.py:155` unused-ignore discrepancy, DEBT-014) |
| Silenced modules (`follow_imports = silent` in mypy.ini) | 5 modules (was 9 → 8.0.1 removed 3, 8.0.2 removed 1) |

The original May 2026 baseline had 32 errors / 12 files. By June 2026 (after Phases 7.15–7.18 work that
kept `mypy .` green but accumulated strict-mode debt), the count had grown to 79 errors / 25 files. The
Phase 8.0.0 sweep closed the 64 surface-fixable errors; the remaining 15 are structurally gated behind
silenced dependencies and cannot be fixed until Phase 8.1–8.4 unblocks their transitive imports.

### Residual errors (`mypy --strict main.py`) — 1 remaining

| File | Count | Error codes | Root cause |
|---|---|---|---|
| `brain/swarms.py` | 1 | `unused-ignore` on `:155` | `tool_rag_select_node` add_node `NodeInputT` discrepancy between `mypy .` and `--strict` modes (DEBT-014) |

> **8.0.1 (2026-06-05):** closed 19 errors (leaves + ideation); 15 → 7 residual. Attribution of
> ideation.py corrected (was not gated by analyst).
> **8.0.2 (2026-06-05):** closed 7 consumer errors (contract_guard, summarizer, coder); 7 → 1
> residual. DEBT-015 and DEBT-016 closed. DEBT-014 updated with strict/non-strict discrepancy note.

### Historical baseline (2026-05-31) — for reference only

| Gate | Result |
|---|---|
| `mypy .` | ✅ Success — 220 files, 0 errors |
| `mypy --strict main.py` | ❌ 32 errors across 12 transitively-imported files |

**8.0.0 closed (2026-06-05):** 64 errors fixed across 20 files. Dominant patterns:
parameterize bare `dict` → `Dict[str, Any]` (35 occurrences); remove stale `# type: ignore`
comments (15); cast `Any` returns to declared type (2 `no-any-return` in `brain/intent_router.py`);
fix `no-untyped-def` in `brain/guardrails.py` + `agents/prompts.py` + `main.py`; remove redundant
`cast()` in `main.py`; resolve `brain/swarms.py` DEBT-003/004. Also fixed: `api/mcp_servers.py`
stale `# type: ignore[attr-defined]` (mcp stubs improved); `core/janitor.py`,
`core/tool_rag.py`, `core/memory/trajectory_memory.py` stale `# type: ignore[import-untyped]`
for `pyarrow` (stubs now present; `lancedb` remains untyped).

### Silenced modules (unknown debt behind the wall)

| Module | Consumers | Priority |
|---|---|---|
| `tools.llm_gateway` | summarizer, contract_guard, coder | High |
| `core.vfs_middleware` | `agents/coder.py` | Medium |
| `core.compute_pool` | `core/indexer.py` + others | High |
| `brain.memory` | `brain/engine.py` | High |
| `core.db` | widespread | Critical |
| `api.websocket_manager` | write_pipeline, finops, main.py | Critical |

*(Unsilenced in 8.0.1: `shared.hardware`, `agents.analyst`, `tools.patch_tool` — DEBT-001 closed.)*
*(Unsilenced in 8.0.2: `tools.llm_gateway` — DEBT-015, DEBT-016 closed.)*

---

## Topological Resolution Map (bottom-up)

```
Tier 0 — Leaves (import only stdlib / third-party / shared.*)
  brain/state.py · brain/checkpoint.py · agents/roles.py
  core/permissions.py · shared/config.py · core/sandbox.py

Tier 1 — Pure nodes (no module-level internal deps)
  brain/guardrails.py · agents/contract_guard.py
  brain/drift_monitor.py · brain/finops.py

Tier 2 — LLM-dependent (blocked by tools.llm_gateway silencing)
  brain/summarizer.py · agents/coder.py · core/memory/trajectory_memory.py

Tier 3 — Subgraph consumers
  brain/ideation.py (blocked by agents.analyst)
  brain/swarms.py · brain/nodes/* · brain/intent_router.py

Tier 4 — Core infra (blocked by core.db + api.websocket_manager)
  core/write_pipeline.py · api/mcp_servers.py · core/tool_rag.py
  core/dead_letter.py · core/telemetry_log.py · core/supervisor.py

Tier 5 — API layer (largely surface type-arg fixes)
  api/byom.py · api/runtime.py · api/hardware.py
  api/audit.py · api/skills.py · api/agent_roles.py
  api/system_settings.py · core/config_generator.py

Tier 6 — Central orchestrator
  brain/engine.py (15 direct internal deps; ready ONLY after all above)

Tier 7 — Entrypoint
  main.py (ready after engine.py)
```

---

## WBS — Subfases

### 8.0 — Mechanical Surface Fixes — ✅ CLOSED 2026-06-05

**Scope:** Eliminate all errors visible under `mypy --strict main.py` that are NOT gated behind
silenced modules. All changes are type-annotation only — zero logic changes.

**DoD:** ✅ `mypy .` → 0 (247 files); `mypy --strict main.py` → 15 residual errors, all gated
behind silenced deps (DEBT-014/015/016). The DoD is met: no surface-fixable error remains.
`pytest` → 924 passed, 0 failed.

**Files fixed and fix pattern (64 errors across 20 files):**

| File | Fix | Errors closed |
|---|---|---|
| `api/system_settings.py` | 10 × `dict` → `Dict[str, Any]`; `_DEFAULTS` var; helper params | 10 |
| `core/config_generator.py` | 5 × `dict`/`List[dict]` → `Dict[str, Any]` variants | 5 |
| `api/audit.py` | 3 × `list[dict]`/`dict` → `List[Dict[str,Any]]`/`Dict[str,Any]` | 3 |
| `api/mcp_servers.py` | Remove stale `# type: ignore[attr-defined]` (mcp stubs fixed); 4 × `dict` | 5 |
| `api/skills.py` | 3 × `dict` → `Dict[str, Any]` | 3 |
| `brain/finops.py` | 3 × `dict` → `Dict[str, Any]` (node sig + route fn + Optional var) | 3 |
| `brain/guardrails.py` | 4 × `dict` → `Dict[str, Any]`; `Optional[dict]` → `Optional[Dict[str,Any]]`; `no-any-return` fix | 4 |
| `api/hardware.py` | 2 × `dict` → `Dict[str, Any]`; remove stale `# type: ignore[arg-type]` | 3 |
| `api/agent_roles.py` | 2 × `dict` → `Dict[str, Any]` | 2 |
| `api/byom.py` | 2 × `dict` → `Dict[str, Any]` (param + local var) | 2 |
| `brain/engine.py` | 3 × `dict` → `Dict[str, Any]`; remove stale `# type: ignore[type-var]` | 4 |
| `brain/intent_router.py` | 2 × `no-any-return` → `cast(Dict[str, Any], await ainvoke(...))` | 2 |
| `brain/swarms.py` | Remove 8 stale `# type: ignore`; `BaseCheckpointSaver[Any]`; add 4 targeted node ignores | 9 |
| `brain/drift_monitor.py` | 1 × `dict` → `Dict[str, Any]` | 1 |
| `core/write_pipeline.py` | 1 × `dict` → `Dict[str, Any]` | 1 |
| `core/janitor.py` | `pyarrow.compute` ignore stale → removed; keep lancedb ignore | 1 |
| `core/tool_rag.py` | `pyarrow` ignore stale → removed; keep lancedb ignore | 1 |
| `core/memory/trajectory_memory.py` | `pyarrow` ignore stale → removed; keep lancedb ignore | 1 |
| `api/runtime.py` | Remove stale `# type: ignore[assignment]` line 107 | 1 |
| `agents/planner.py` | Remove stale `# type: ignore[assignment]` line 550 | 1 |
| `main.py` | Remove 2 × redundant `cast()` (DreamingRunPayload, IdeTelemetryPayload) | 2 |

**Also resolved in this sweep:** DEBT-003 (`BaseCheckpointSaver[Any]`), DEBT-004 (stale swarms.py
ignores). Pyarrow stubs landed — all pyarrow `# type: ignore[import-untyped]` comments removed.
Lancedb stubs still absent — its ignores retained.

---

### 8.0.FE — Frontend TypeScript/Pylance gate (permanent certification)

**Scope:** There is no TypeScript strict-type campaign equivalent to the Python 8.0.x series — the
frontend compiler already enforces full types (no `strict: false` escape hatch; `tsc --noEmit` is the
check-types gate). This sub-phase simply documents the permanent frontend gate and tracks the 2
pre-existing ESLint warnings.

**DoD:** `npm run compile` (= `tsc --noEmit` + `eslint src` + `node esbuild.js`) → exit 0. ✅
Already green — the enforced gate on every compile.

**Existing debt:**
- 2 ESLint `semi` warnings in `api/api_client.ts` and `editor/vfs_reader.ts` — auto-fixable
  (`eslint src --fix`). Register as DEBT-017 when addressed.
- No open TypeScript strict errors. Bundle ceiling sentinel (`assertWebviewBundleUnderCeiling()` in
  `esbuild.js`, production-only) flags any regression before it ships.

**What it is NOT:** a campaign of strictness tightening (TypeScript already enforces `strict: true`
by default in the tsconfig). The frontend track is "keep the green lights green" rather than
"unsilence progressively."

---

### 8.1 / 8.0.1 — Unsilence Low-Fan-In Leaves — ✅ CLOSED 2026-06-05

**Scope:** Removed `follow_imports = silent` for the three modules with ≤1 internal consumer, in
topological order. All fixes annotation-only — zero logic changes. `brain/ideation.py` was folded in
(its 8 errors were self-contained, not gated by `analyst` — attribution corrected).

| File | Strict errors fixed | Fix |
|---|---|---|
| `shared/hardware.py` | 3 × `unused-ignore` | drop 2 stale psutil ignores (config-silenced); pynvml `[import]`→`[import-untyped]` |
| `agents/analyst.py` | 8 × `type-arg` | parameterize bare `set`→`Set[asyncio.Task[Any]]`, `dict`/`Dict`→`Dict[str, Any]`; add `Any, Set` imports |
| `tools/patch_tool.py` | 1 × `unused-ignore` | drop stale `@tool` ignore — LangChain stubs caught up → **DEBT-001 closed** |
| `brain/ideation.py` (folded in) | 8 × `type-arg` | bare `dict`→`Dict[str, Any]`; `StateGraph`→`StateGraph[AIlienantGraphState]` |
| `brain/swarms.py` | 1 × `unused-ignore` | removed a `# type: ignore[type-var]` (line 155) that went dead once `analyst` became typed (`add_node` overload re-resolved) |

`mypy.ini`: removed `[mypy-shared.hardware]`, `[mypy-agents.analyst]`, `[mypy-tools.patch_tool]` (9 → 6).

**DoD met:** `mypy --strict` → 0 on all four leaves; `mypy .` → 0/247; `mypy --strict main.py`
15 → 7 (residual behind `tools.llm_gateway`); `pytest` → 924 passed, 0 failed.

---

### 8.2 / 8.0.2 — Unsilence `tools.llm_gateway` — ✅ CLOSED 2026-06-05

**Scope:** `tools/llm_gateway.py` was already strict-clean internally (0 errors); the `follow_imports
= silent` block was shielding its *consumers*. Once the block was removed, the 7 consumer errors
became visible and were fixed. `MODEL_MEDIUM` was not re-exported by `llm_gateway`; the fix was to
import it from `shared.config` directly in `contract_guard.py` (proper canonical source).

| File | Fix |
|---|---|
| `agents/contract_guard.py` | `from tools.llm_gateway import LLMGateway, MODEL_MEDIUM` → import `MODEL_MEDIUM` from `shared.config` |
| `brain/summarizer.py` | `state: dict` → `Dict[str, Any]`; added `Any` to imports |
| `agents/coder.py` | `Set[asyncio.Task[Any]]` + `_make_vfs_reader -> Callable[[str], Optional[str]]` + 3 × `Dict[str, Any]`; added `Any, Callable, Dict, Set` |
| `brain/swarms.py` | restored `# type: ignore[type-var]` on `:155` (needed by `mypy .`; `--strict` mode sees it as unused — DEBT-014 discrepancy) |
| `mypy.ini` | removed `[mypy-tools.llm_gateway] follow_imports = silent` (6 → 5) |

**DoD met:** `mypy --strict tools/llm_gateway.py` → 0; `mypy .` → 0/247; `mypy --strict main.py`
7 → 1 (swarms.py:155, DEBT-014); `pytest` → 924/0. DEBT-015 closed, DEBT-016 closed.

---

### 8.3 — Unsilence `core.vfs_middleware` + `core.compute_pool`

**Subfases:**

#### 8.3.A — `core.vfs_middleware`
- Fix errors in `core/vfs_middleware.py` until `mypy --strict core/vfs_middleware.py` → 0.
- Remove silencing entry.
- **DoD:** `mypy --strict core/vfs_middleware.py` → 0; `mypy --strict agents/coder.py` → 0.

#### 8.3.B — `core.compute_pool`
- Fix errors in `core/compute_pool.py` until `mypy --strict core/compute_pool.py` → 0.
- Remove silencing entry.
- **DoD:** `mypy --strict core/compute_pool.py` → 0; `mypy --strict core/indexer.py` → 0.

---

### 8.4 — Tier 2 + Tier 3 Nodes (unlocked by 8.1–8.3)

**Scope:** With llm_gateway, vfs_middleware, compute_pool, and analyst unsilenced, these modules
can now be made strict-clean. Execute in dependency order (deps before consumers).

**Order within this phase:**
1. `brain/summarizer.py`
2. `agents/coder.py`
3. `core/memory/trajectory_memory.py`
4. `brain/ideation.py`
5. `brain/nodes/circuit_breaker.py` → `brain/swarms.py` (fixes DEBT-003, DEBT-004)
6. `brain/intent_router.py`

**DoD:** `mypy --strict <file>` → 0 for each, confirmed sequentially. `mypy .` stays clean.

---

### 8.5 — Unsilence `brain.memory` + `core.db`

**Scope:** The two most pervasive silenced modules — unsilencing them is the hardest step and the
highest-value unlock. Assess the hidden debt first with exploratory runs before committing to fixes.

#### 8.5.A — `brain.memory`
- Exploratory: `mypy --strict brain/memory.py 2>&1 | head -60` to count errors.
- Fix errors. Remove silencing entry.
- **DoD:** `mypy --strict brain/memory.py` → 0.

#### 8.5.B — `core.db`
- Exploratory: `mypy --strict core/db.py 2>&1 | head -60`.
- Fix errors (this module has raw SQLite; likely `Optional` and `Any` typing work).
- Remove silencing entry.
- **DoD:** `mypy --strict core/db.py` → 0.

---

### 8.6 — Unsilence `api.websocket_manager` + Core Infra

**Scope:** The last infrastructure wall. Fixes `core/dead_letter.py`, `core/telemetry_log.py`,
`core/supervisor.py` (all needed by engine) and unsilences the WebSocket manager.

**Order:**
1. `core/dead_letter.py`, `core/telemetry_log.py`, `core/supervisor.py`
2. `api/websocket_manager.py` — exploratory run first; fix; remove silencing.

**DoD:** each file `mypy --strict <file>` → 0; all three silencing entries removed.

---

### 8.7 — `brain/engine.py` (Central Orchestrator)

**Scope:** All 15 direct internal deps must be strict-clean before this phase starts.
Re-assess E402 deferred imports: if unsilencing the transitive graph has resolved the circular-import
risk, move deferred top-level imports back to module scope for full type coverage.

**DoD:** `mypy --strict brain/engine.py` → **exit 0**.

---

### 8.8 — `main.py` (Final Entrypoint Gate)

**Scope:** Final cleanup pass. All imported modules are now typed; this should reduce to
cosmetic annotation work.

**DoD:**
- `mypy --strict main.py` → **exit 0** — the full campaign target.
- All `follow_imports = silent` entries removed from `mypy.ini` (or only `tools.patch_tool`
  remains if DEBT-001 is still externally blocked).
- `mypy .` still clean.

---

## Continuous Registry Protocol

> **Rule:** If you discover a strict-mode error, vulnerability, or typing debt OUTSIDE the
> current subfase's scope, you MUST register it in `docs/TECH_DEBT_BACKLOG.md` immediately
> and NOT fix it in-place. This applies to ALL future interactions on this codebase.

See `docs/TECH_DEBT_BACKLOG.md` for current entries (DEBT-001 through DEBT-005 pre-registered
from the Phase 8 audit).

---

## Verification Commands (Reference)

```powershell
cd C:\Proyectos\Proyect_Ailienant\ailienant-core

# Enforced project gate (must always stay green)
.\venv\Scripts\python -m mypy . 2>&1 | tail -3

# Phase 8.0 gate (new gate after 8.0 completes)
.\venv\Scripts\python -m mypy --strict main.py 2>&1 | tail -5

# Per-subfase leaf gates (examples)
.\venv\Scripts\python -m mypy --strict brain/daemon.py          # already clean
.\venv\Scripts\python -m mypy --strict api/ws_contracts.py      # already clean
.\venv\Scripts\python -m mypy --strict shared/hardware.py       # 8.1.A target
.\venv\Scripts\python -m mypy --strict agents/analyst.py        # 8.1.B target
.\venv\Scripts\python -m mypy --strict tools/llm_gateway.py     # 8.2 target
.\venv\Scripts\python -m mypy --strict core/vfs_middleware.py   # 8.3.A target
.\venv\Scripts\python -m mypy --strict core/compute_pool.py     # 8.3.B target
.\venv\Scripts\python -m mypy --strict brain/engine.py          # 8.7 target
```
