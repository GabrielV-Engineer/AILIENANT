# PHASE 4 — Master Architectural Blueprint

> **Mandatory read** during every Phase 4 task. Survives session compactions: re-derive intent from this document.

## Context

Phase 3 (Memoria Evolutiva, MCTS, Janitor, Fast-Boot, Hybrid Cascading) closed the **cognitive substrate**: the system now knows *where to think* (LOCAL/CLOUD tiers, MCTS branches), *what it knows* (GraphRAG + LanceDB + Dual-Rules), and *how to forget* (Janitor GC, AGENTS.md fast-boot). Phase 4 closes the **executive layer**: how that substrate is actually driven during a coding mission.

Phase 4 must deliver three guarantees:

1. **Single Model, Many Personalities** — one CoderAgent in VRAM that transmutes via Prompt Swapping (8 RBAC roles), avoiding the cost of holding multiple specialists resident.
2. **Three Execution Modes with Deterministic Transitions** — Sequential (bypass), Micro-Swarm (ReAct + deterministic validators), Full Swarm (Researcher → Planner → Orchestrator → Coder swarm → Analyst).
3. **Bounded Failure** — every loop has a numeric ceiling and every ceiling triggers either the Cloud Surgeon, the Give-Up Gate, or HITL. No infinite token bleed.

This blueprint is the contract that future Phase 4 PRs must conform to; later compactions can drop session memory and still re-derive intent from this document.

---

## 1. The Unified State Contract (`AIlienantGraphState`)

Phase 4 **extends** the existing TypedDict in [ailienant-core/brain/state.py](../ailienant-core/brain/state.py); it does not replace it. Fields already present are referenced verbatim; new fields are marked `# Phase 4 ADD`. Reducers stay compatible with parallel `Send()` fan-out from `route_to_coders`.

```python
class AIlienantGraphState(TypedDict):
    # === Identity (existing) ===
    task_id: str
    user_input: str
    project_id: Optional[str]
    workspace_root: Optional[str]
    explicit_mentions: List[str]
    attachments: List[ManualAttachment]

    # === Conversational memory (existing) ===
    messages: Annotated[List[Dict[str, str]], _merge_messages]

    # === Telemetry / Routing (existing, sourced from ContextMeter) ===
    context_metrics: ContextMeter          # carries css_total, tci, is_red_alert, routing_decision
    active_llm_profile: LLMProfile
    token_usage: TokenCounter
    tci: float                              # 0..100  — Task Complexity Index shortcut
    css: float                              # 0..100  — Context Sufficiency Score shortcut
    has_images: bool
    routing_warning: Optional[str]
    hardware_profile: Optional[HardwareProfile]
    provider: str                           # "LOCAL" | "CLOUD" | "HUMAN_REQUIRED"

    # === Prompt-Swapping pointer (existing, extended) ===
    target_role: Optional[str]              # Phase 4 EXTENDS Literal set (see §3)
    current_step_id: Optional[int]
    is_manual_override: bool

    # === Mission spec (existing) ===
    mission_spec: Optional[MissionSpecification]
    immutable_wbs: Optional[MissionSpecification]   # Shadow planner baseline
    parallel_tasks: List[WBSStep]

    # === HITL & Planner Mode (existing) ===
    planner_mode_active: bool
    hitl_pending: bool
    hitl_response: Optional[str]
    shared_understanding_reached: bool

    # === VFS (existing) ===
    read_files_state: Dict[str, VFSFile]
    vfs_buffer: Annotated[Dict[str, VFSFile], _merge_vfs]
    generated_code: Annotated[Dict[str, VFSFile], _merge_generated_code]
    pending_patches: Annotated[Dict[str, str], operator.or_]

    # === Resilience (existing) ===
    errors: Annotated[List[str], operator.add]
    retry_count: int                        # Per-step coder retries (cap = 2)
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str
    session_delta: str
    is_indexing_complete: bool

    # === Guardrails (existing) ===
    guardrail_failed: bool
    validation_feedback: Optional[str]

    # === FinOps (existing) ===
    current_cost_usd: Annotated[float, operator.add]
    max_budget_usd: float

    # ==========================================================
    # === Phase 4 ADD — Executive Layer Channels ===============
    # ==========================================================

    execution_mode: Literal["SEQUENTIAL", "MICRO_SWARM", "FULL_SWARM"]
    # Decided once by IntentRouter at graph entry; locked for the lifetime of the run
    # to prevent mid-mission topology shifts (KV cache invalidations are expensive).

    active_role: Optional[Literal[
        "core_dev", "architect_refactor", "devops_infra",
        "secops", "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer"
    ]]
    # Concrete RBAC personality currently bound to CoderAgent. After §3.1 schema
    # widening, this is a 1:1 copy of WBSStep.target_role for the active step
    # (legacy 5-value checkpoints are migrated by a Pydantic before-validator).

    error_streak: int                       # Consecutive validator failures on the SAME step
    # Distinct from retry_count: retry_count resets on step transition; error_streak is
    # the input to the Circuit Breaker (escalates to Cloud Surgeon at 3, see §4).

    consecutive_style_failures: int         # Style-gate-only failures (lint, not syntax)
    # Drives "Give Up" Gate (§4): at 2 → STYLE_BYPASS_ACTIVATED, hand off to Analyst.

    style_bypass_active: bool               # Latched once Give-Up Gate fires; informs Analyst.

    syntax_gate_status: Literal["pass", "fail", "pending"]
    style_gate_status: Literal["pass", "fail", "pending"]
    # Split outcomes so the router can distinguish "broken code" from "ugly code".

    relaxed_typing_mode: bool               # Pre-flight: missing 3rd-party stubs → relax mypy.
    venv_interpreter_path: Optional[str]    # From VS Code activeInterpreter MCP endpoint.

    circuit_breaker_tripped: bool           # True once Local tier exhausted; forces Cloud Surgeon.
    cloud_surgeon_invocations: int          # Hard cap = 1 per step to prevent recursive escalation.

    workspace_pid: Optional[int]            # VS Code window PID for Lifecycle Manager (§4.6).
    workspace_active: bool                  # Set False by Lifecycle Manager → triggers shutdown.

    soul_md_hash: Optional[str]             # Hash of ~/.ailienant/SOUL.md at load time.
    # Only the AnalystAgent reads SOUL.md; cached by hash for hot-reload without restart.
```

### Field Provenance Map

| Field | Written by | Read by | Reducer |
|---|---|---|---|
| `execution_mode` | `IntentRouter` (entry) | All routers | last-write (locked) |
| `active_role` | `OrchestratorAgent` per step | `CoderAgent` (prompt build), `tool_filter` | last-write |
| `error_streak` | Validators (Syntax/Style gates) | Circuit Breaker | last-write (reset on step++) |
| `consecutive_style_failures` | StyleGate node | Give-Up Gate | last-write (reset on step++) |
| `circuit_breaker_tripped` | Circuit Breaker | `route_to_coders` | latch (never resets within run) |
| `cloud_surgeon_invocations` | Cloud Surgeon | Circuit Breaker | `operator.add` |
| `workspace_pid` / `workspace_active` | Lifecycle MCP listener | Shutdown subgraph | last-write |

---

## 2. Deterministic Routing Logic

All three modes share the same entry point: **`IntentRouter`** — a deterministic node (no LLM, O(1)) that classifies the user prompt + manual toggles + CSS into `execution_mode`. From then on, the topology is fixed for the run.

### 2.1 IntentRouter (entry classifier)

```
INPUT  : user_input, planner_mode_active, css, tci, has_images, attachments
OUTPUT : execution_mode

# Decision cascade (first match wins):
if planner_mode_active                       → FULL_SWARM     # explicit user toggle
elif is_red_alert (css < 40)                 → FULL_SWARM     # need Researcher to recover context
elif has_images                              → FULL_SWARM     # vision payload demands cloud planner
elif tci >= 75                               → FULL_SWARM     # complex arch — needs Planner Heavy
elif 30 <= tci < 75                          → MICRO_SWARM    # localizable mutation w/ validation
elif tci < 30 AND single-turn intent regex   → SEQUENTIAL     # "explain this", "what does X do"
else                                         → MICRO_SWARM    # safe default
```

> Numeric thresholds reuse the existing `RoutingEngine` constants (CSS<40, TCI 30/40/75) — no new magic numbers introduced.

### 2.2 SEQUENTIAL Mode (Bypass — 1–3 s latency target)

Pure ReAct loop with **LangGraph disabled**. No SQLite checkpoint, no nodes, no fan-out. Single in-process call.

```
[User Prompt]
     │
     ▼
IntentRouter ── SEQUENTIAL ──▶ AnalystAgent (read-only tools only)
                                     │
                                     ▼
                              [Streamed answer → WebSocket]
                                     │
                                     ▼
                                  [User]
```

- **Allowed tools:** `FileReadTool`, `GrepTool`, `query_graphrag`, `RunLinterTool` (read-only).
- **Forbidden:** `apply_patch`, `BashTool`, `BatchEditTool`.
- **State writes:** only `messages` + `token_usage`. No checkpoint persisted.
- **Termination:** single LLM turn. No retry loop.

### 2.3 MICRO_SWARM Mode (ReAct loop — bounded)

One cognitive agent (CoderAgent in its current `target_role`) talking to **deterministic Python validators**. No multi-LLM chatter.

```
[User Prompt]
     │
     ▼
IntentRouter ── MICRO_SWARM ──▶ verify_environment (4.2.2 pre-flight)
                                     │
                                     ▼
                              ┌───────────────┐
                              │  CoderAgent   │◀────────────────┐
                              │ (active_role) │                 │
                              └───────┬───────┘                 │
                                      │ tool_call: apply_patch  │
                                      ▼                         │
                              ┌───────────────┐                 │
                              │  SyntaxGate   │  ast.parse /    │
                              │ (4.2.x)       │  tree-sitter    │
                              └───────┬───────┘                 │
                                fail  │  pass                   │
                              ┌───────┴──────┐                  │
                              ▼              ▼                  │
                        inject error    ┌────────────┐          │
                        to messages     │ StyleGate  │          │
                        retry_count++   │ ruff/eslint│          │
                              │         └─────┬──────┘          │
                              │         fail  │  pass           │
                              │      ┌────────┴─────┐           │
                              │      ▼              ▼           │
                              │  GiveUpGate    Persist VFS      │
                              │  (4.2.3)             │          │
                              │      │               ▼          │
                              │      ▼          step++ / done   │
                              └──────────────────────────────────
                                  (retry_count < 2)
```

- **Retry budget:** `retry_count <= 2` for the current step.
- **Validator order:** SyntaxGate first (cheap, deterministic), StyleGate second (skippable via GiveUpGate).
- **Escalation:** `error_streak == 3` → Circuit Breaker → Cloud Surgeon (single shot, see §4).
- **No Planner/Researcher/Analyst** in this mode — the WBS is implicit (single atomic step inferred from user intent).

### 2.4 FULL_SWARM Mode (Enterprise Bicephalous)

Full LangGraph topology with SQLite checkpointing. The Planner is the only "Heavy/Opus" node; everything else is Sonnet-class.

```
[User Prompt]
        │
        ▼
   IntentRouter ── FULL_SWARM
        │
        ▼
  ResearcherAgent (4.1.1)
   ├── query_graphrag (LanceDB+NetworkX k-hop)
   ├── GlobTool / GrepTool
   └── EntropyPayload bypass for @-mentions
        │ Skeleton Prompt (signatures + relations, NOT full files)
        ▼
   PlannerAgent (4.1.2 — Opus, runs once O(1))
   ├── with_structured_output → MissionSpecification
   ├── Polyglot Surgical Strike guard (Phase 1.0.5 carryover)
   └── Freeze immutable_wbs (Shadow Planner baseline)
        │
        ▼
   OrchestratorAgent (4.1.3 — capataz, O(N) loop)
   │ for step in mission_spec.tasks:
   │     ├── set active_role from step.target_role  (Prompt Swapping)
   │     ├── set current_step_id
   │     ├── recompute 3D Routing matrix (CSS/TCI/HW)
   │     ▼
   │   ┌──── route_to_coders (Send fan-out if parallel_tasks not empty) ────┐
   │   │                                                                     │
   │   ▼                                                                     ▼
   │  CoderAgent[role_A]                                              CoderAgent[role_B]
   │   │   (Micro-Swarm sub-loop, §2.3)                                   │
   │   └──────── _merge_generated_code / _merge_vfs reducers ─────────────┘
   │     │
   │     ▼
   │   DriftMonitor (compare mission_spec ↔ immutable_wbs)
   │     │ if drift > threshold → HITL_APPROVAL_REQUIRED
   │     ▼
   │   apply_patch_node (consumes pending_patches)
   │     │
   │     ▼
   │   step.status = completed / failed
   │
        ▼ (all steps done)
   AnalystAgent (4.1.5 — Final report + SOUL.md persona)
        │
        ▼
   [User: Mission report + diff approvals via WorkspaceEdit IPC]
```

- **Determinism guarantee:** Orchestrator never invokes an LLM for routing — only `RoutingEngine.resolve_provider()`. The only LLM call per step is the CoderAgent itself.
- **Parallelism:** `parallel_tasks` enables `Send()` fan-out; reducers (`_merge_vfs`, `_merge_generated_code`, `operator.add`) prevent state collisions — already proven in Phase 1.0.4.
- **Checkpoint:** every node transition writes to SQLite WAL; HITL pauses suspend the graph without blocking FastAPI threads.

---

## 3. The Prompt Swapping Matrix

One model in VRAM. The System Prompt + tool array are mutated per step. Source-of-truth file: `ailienant-core/prompts/roles.py` (to be created in Phase 4.1.4).

### 3.1 Schema extension — `WBSStep.target_role` widened to 8 values

**Decision:** Extend the existing Literal in [ailienant-core/brain/state.py:26-29](../ailienant-core/brain/state.py#L26-L29) so the Planner can request any of the 8 RBAC roles explicitly. This is a deliberate `SCHEMA_EVOLUTION.MD` change.

```python
class WBSStep(BaseModel):
    step_number: int
    target_role: Literal[
        "core_dev", "architect_refactor", "devops_infra",
        "secops", "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer"
    ] = Field(
        default="core_dev",
        description="The role ('System Prompt') the CoderAgent assumes for this step.",
    )
    ...
```

**Backward compatibility for old checkpoints** (mission specs persisted under the legacy 5-value vocabulary):

| Legacy value | Migrated to | Migration site |
|---|---|---|
| `Refactor` | `architect_refactor` | Pydantic `model_validator(mode="before")` on `WBSStep` |
| `Infra` | `devops_infra` | same |
| `Doc` | `doc_manager` | same |
| `SecOps` | `secops` | same |
| `Test` | `qa_tester` | same |

The migration validator runs only on deserialization; new code emits only the 8-value vocabulary. After one full release cycle the validator is removed (debt item logged in `PROJECT_MANIFEST.md` under "Tech Debt Cleanup").

**Implicit-trigger roles** (no legacy precedent): `core_dev` is the new default; `vcs_manager` and `data_ml_engineer` must be assigned by the Planner directly — they have no legacy mapping and no automatic heuristic. Earlier drafts proposed regex-based heuristics for these; that has been **rejected** to keep role assignment fully explicit and auditable.

> `MissionSpecification` itself is unchanged; only `WBSStep.target_role`'s Literal widens. `SCHEMA_EVOLUTION.MD` gets a new entry: "Phase 4.0 — WBSStep.target_role widened from 5 to 8 values; legacy values auto-migrated via Pydantic before-validator."

### 3.2 RBAC Matrix (Tools × Roles)

Legend: ✅ allowed, ⛔ forbidden, ⚠️ HITL gate required, 🔒 ReadOnly only.

| Tool | core_dev | architect_refactor | devops_infra | secops | qa_tester | doc_manager | vcs_manager | data_ml_engineer |
|---|---|---|---|---|---|---|---|---|
| `FileReadTool` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `GrepTool` / `GlobTool` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `query_graphrag` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `apply_patch` (SEARCH/REPLACE) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (`.md`/docstrings only) | ⛔ | ✅ |
| `BatchEditTool` | ⛔ | ✅ **(exclusive)** | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ |
| `WriteFileTool` (full-file) | ✅ | ⛔ **(SOLID constraint)** | ✅ | ⛔ | ⛔ | ✅ (`.md` only) | ⛔ | ✅ |
| `BashTool` (non-sudo) | ⚠️ | ⛔ | ✅ | ⛔ | ✅ (test runners only) | ⛔ | ✅ (git only) | ✅ (notebooks/pip-in-venv) |
| `BashTool` (sudo / `.env`) | ⛔ | ⛔ | ⚠️ **HITL** | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ |
| `RunLinterTool` | ✅ | ✅ | ✅ | ✅ **(Bandit/Semgrep injected)** | ✅ **(must consume stderr)** | 🔒 | 🔒 | ✅ |
| `pytest` runner | ✅ | ✅ | ✅ | ✅ | ✅ | ⛔ | ⛔ | ✅ |
| Git mutating (`commit`, `push`, `rebase`) | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ | ✅ | ⛔ |
| Multimodal `DocumentParserTool` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

### 3.3 Per-role System Prompt deltas (semantic, not literal)

| Role | Prompt directive (concatenated to base) | Forbidden phrases |
|---|---|---|
| `core_dev` | "Implement business logic. Prefer existing utilities. No abstractions for hypothetical futures." | — |
| `architect_refactor` | "SOLID enforced. You MUST use `BatchEditTool`. Rewriting whole files is a contract violation." | "rewrite file", "from scratch" |
| `devops_infra` | "You touch Docker/CI/Bash. Any sudo or `.env` mutation pauses for HITL." | (silent) |
| `secops` | "OWASP Top-10 enforced. Run Bandit/Semgrep after every patch. Quote CVE IDs when relevant." | (silent) |
| `qa_tester` | "Write tests first. NEVER mark step complete without `pytest exit 0`. Always read stderr." | "this test is too hard to write" |
| `doc_manager` | "JSDoc / docstrings / `.md` only. `BashTool` disabled. Never touch logic." | (silent) |
| `vcs_manager` | "Git operations only. Conventional Commits format. Never `--force` without HITL." | "--force-with-lease" (HITL) |
| `data_ml_engineer` | "Tensors, pipelines, analytics. Validate dataframe shapes pre-write." | "trust the data" |

### 3.4 AnalystAgent — Cognitive Isolation (Alma de La Hormiga)

The AnalystAgent is the **only** node permitted to load `~/.ailienant/SOUL.md`. The Planner, Researcher, Orchestrator, and Coder explicitly skip SOUL.md loading at prompt-build time. Enforcement:

- Implementation hook: a single `load_soul_md()` function lives in `agents/analyst.py`. Other agents importing it triggers a `mypy` no-public-re-export rule (architectural fence).
- Hot-reload: `soul_md_hash` in state acts as the cache key; mismatch forces re-read.

---

## 4. Escalation & Break Boundaries

Every loop has a numeric ceiling. Every ceiling triggers exactly one next-step transition.

### 4.1 Numeric thresholds (canonical)

| Boundary | Symbol | Value | Source |
|---|---|---|---|
| **CSS Red-Alert** | `RED_ALERT_CSS` | < 40.0 | Existing — `routing_engine.resolve_provider` |
| **TCI tiers** | — | <30 / 30–75 / ≥75 | Existing — `routing_engine.select_best_agent` |
| **Coder retry cap (per step)** | `MAX_RETRIES` | 2 | Phase 1.0.5 Checkpoint Gate carryover |
| **Error streak → Cloud Surgeon** | `CIRCUIT_BREAKER_THRESHOLD` | 3 | Phase 3.4.8 Hybrid Cascading |
| **Style-only failures → Give-Up Gate** | `STYLE_BYPASS_THRESHOLD` | 2 | New (4.2.3) |
| **Cloud Surgeon invocations per step** | `MAX_CLOUD_SURGEON` | 1 | New (anti-recursion) |
| **Token budget per mission (default)** | `max_budget_usd` | env: `AILIENANT_MAX_BUDGET_USD` | Existing — `finops_gate` |
| **Context window safety margin** | `CONTEXT_BUFFER` | 20% (use ≤80%) | Existing — `RoutingEngine.select_best_agent` |
| **State summariser threshold** | `SUMMARY_TRIGGER` | 80% window | Phase 2.1.11 |
| **Cognitive Horizon (preserved turns)** | — | last 5 | Phase 2.1.11 |
| **Lifecycle workspace-idle timeout** | `WORKSPACE_IDLE_SEC` | 300 | New (4.4) |

### 4.2 The "Give Up" Gate (Style Bypass) — 4.2.3

```
Condition (all must hold):
  syntax_gate_status == "pass"         # code parses
  AND style_gate_status == "fail"      # only style/lint complains
  AND consecutive_style_failures >= STYLE_BYPASS_THRESHOLD (= 2)

Action:
  state.style_bypass_active = True
  state.security_flags += ["STYLE_BYPASS_ACTIVATED"]
  edge → AnalystAgent
  AnalystAgent receives banner: "Code is syntactically valid but failed style 2x. Style hostile — surfacing diff to user for manual triage."
  CoderAgent loop EXITS (no further retries for this step)
```

Rationale: An infinite loop of "fix linter complaint → linter shifts complaint" is worse than handing off a working-but-ugly diff. Latch protects users from token bleed against hostile lint configs.

### 4.3 Circuit Breaker (Local → Cloud Surgeon) — 4.3 + 3.4.8 reuse

```
Condition:
  error_streak >= CIRCUIT_BREAKER_THRESHOLD (= 3)
  AND cloud_surgeon_invocations < MAX_CLOUD_SURGEON (= 1)
  AND current_cost_usd + estimated_surgeon_cost <= max_budget_usd

Action:
  state.circuit_breaker_tripped = True
  state.provider = "CLOUD"
  state.active_llm_profile = LLMProfile(tier="CLOUD_SURGEON")
  edge → CoderAgent (rebound with CLOUD model, same active_role)
  on success: state.error_streak = 0, continue
  on second failure even after Surgeon: edge → AnalystAgent with banner "CLOUD_SURGEON_EXHAUSTED" → graceful failure

Side effects:
  TokenLedger.cloud += delta            # FinOps audit
  cloud_surgeon_invocations += 1
```

### 4.4 Budget Gate (Cost Override) — reuses Phase 1.0.4 `finops_gate`

If `current_cost_usd + projected_step_cost > max_budget_usd`:
- Reject mutation **before** `apply_patch_node` runs.
- Edge → HITL with banner `BUDGET_EXCEEDED`.
- User can: top-up budget (resume), abort (graceful __end__), or downgrade tier (re-enter MICRO_SWARM).

### 4.5 HITL Suspension boundaries

| Trigger | Source node | Resume protocol |
|---|---|---|
| `devops_infra` sudo / `.env` mutation | Permission interceptor | User approves → resume from same step |
| `vcs_manager` `--force` push | Permission interceptor | User approves → resume |
| DriftMonitor: `mission_spec` drifted from `immutable_wbs` beyond similarity threshold | Orchestrator | User confirms drift OK or aborts |
| `BUDGET_EXCEEDED` | finops_gate | User tops-up or aborts |
| `CLOUD_SURGEON_EXHAUSTED` | Circuit Breaker | User reviews → manual fix or skip step |

### 4.6 Lifecycle / PID Manager — 4.4

- On graph entry, MCP endpoint registers `workspace_pid` from VS Code.
- Listener subscribes to VS Code window-close / workspace-change events.
- On disconnect:
  - Mark `workspace_active = False`.
  - Cancel running `Mirror Dreaming` subprocesses (MCTS branches).
  - Issue `PRAGMA wal_checkpoint(TRUNCATE)` on `ailienant_state.sqlite`.
  - Free VRAM (`keep_alive=0` on the active Ollama model).
- **Distinction:** this is *workspace-scoped*, orthogonal to the *process-scoped* WAL shutdown hook from Phase 2.5/2.15.

---

## 5. Critical Files (Touched in Phase 4)

| File | Action | Reason |
|---|---|---|
| [ailienant-core/brain/state.py](../ailienant-core/brain/state.py) | EXTEND + SCHEMA WIDEN | Add Phase 4 channels (§1) **and** widen `WBSStep.target_role` Literal from 5 to 8 values with legacy migration validator (§3.1). |
| `docs/SCHEMA_EVOLUTION.MD` | APPEND | New entry: "Phase 4.0 — WBSStep.target_role widened (5 → 8 values, legacy auto-migrated)." |
| [ailienant-core/brain/engine.py](../ailienant-core/brain/engine.py) | EXTEND | Wire IntentRouter, three subgraphs, mode-locked routing edges. |
| [ailienant-core/brain/routing_engine.py](../ailienant-core/brain/routing_engine.py) | REUSE | All thresholds (CSS<40, TCI 30/75) come from here. No new magic numbers. |
| `ailienant-core/agents/researcher.py` | NEW | Phase 4.1.1 — Skeleton Prompt generator. |
| [ailienant-core/agents/planner.py](../ailienant-core/agents/planner.py) | EXTEND | Phase 4.1.2 — already has structured output + fast-boot; add immutable_wbs freeze. |
| `ailienant-core/agents/orchestrator.py` | NEW | Phase 4.1.3 — capataz loop, prompt-swap dispatcher. |
| `ailienant-core/agents/coder.py` | NEW | Phase 4.1.4 — single CoderAgent with role-mutating prompt builder. |
| [ailienant-core/agents/mcts_coder.py](../ailienant-core/agents/mcts_coder.py) | REUSE | Phase 3.4.8 fail-fast local fixer = the MICRO_SWARM inner loop. |
| `ailienant-core/agents/analyst.py` | NEW (companion) | Phase 4.1.5 — `load_soul_md()` lives here exclusively. |
| `ailienant-core/prompts/roles.py` | NEW | Phase 4.1.4 — System Prompt fragments per role + tool whitelists. |
| `ailienant-core/validators/syntax_gate.py` | NEW | Phase 4.2.x — `ast.parse` + tree-sitter wrapper. |
| `ailienant-core/validators/style_gate.py` | NEW | Phase 4.2.x — ruff/eslint subprocess wrappers (stateless). |
| `ailienant-core/validators/give_up_gate.py` | NEW | Phase 4.2.3 — Style bypass latch. |
| `ailienant-core/validators/env_introspect.py` | NEW | Phase 4.2.1/4.2.2 — Venv proxy + relaxed_typing detection. |
| `ailienant-core/core/lifecycle_manager.py` | NEW | Phase 4.4 — PID listener + graceful workspace shutdown. |
| `ailienant-core/intent_router.py` | NEW | Mode selector entry node. |
| [docs/SYSTEM_PROMPTS.md](SYSTEM_PROMPTS.md) | EXTEND | Document the 8 RBAC personalities verbatim. |
| [docs/PROJECT_MANIFEST.md](PROJECT_MANIFEST.md) | UPDATE | Mark 4.1–4.5 sub-items complete as they land. |
| [docs/DEV_JOURNAL.md](DEV_JOURNAL.md) | APPEND | Hito entry per sub-phase. |

### Reused (not modified) Phase 3 artifacts

- [core/state_manager.py](../ailienant-core/core/state_manager.py) — Fast-Boot AGENTS.md sentinel.
- [core/janitor.py](../ailienant-core/core/janitor.py) — vector GC (called by Lifecycle Manager idle hook).
- [brain/mcts/tree.py](../ailienant-core/brain/mcts/tree.py) — Mirror Dreaming branches; cancelled by Lifecycle 4.4.
- `core/llm_gateway.py` + `TokenLedger` — Tier.LOCAL/Tier.CLOUD dispatch for Circuit Breaker.

---

## 6. Verification Plan (Phase 4.5 Checkpoint Gate)

End-to-end checks executed via `pytest ailienant-core/tests/`:

1. **Mode-Lock Determinism** — `test_intent_router_determinism.py`: feed 12 canonical prompts × {planner_on/off, css high/low, tci low/mid/high, has_images}; assert one and only one `execution_mode` per cell.
2. **SEQUENTIAL Tool Sandbox** — `test_sequential_bypass.py`: assert that calling `apply_patch` while `execution_mode == SEQUENTIAL` raises `PermissionDenied`.
3. **MICRO_SWARM Retry Cap** — `test_micro_swarm_retry_cap.py`: mock CoderAgent to fail syntax 5× → assert exit at retry 2 with `error_streak == 2`, no further LLM calls.
4. **Circuit Breaker Single-Shot** — `test_circuit_breaker.py`: force `error_streak = 3`; assert `cloud_surgeon_invocations` rises to exactly 1; force second failure; assert HITL banner `CLOUD_SURGEON_EXHAUSTED`.
5. **Give-Up Gate Latch** — `test_give_up_gate.py`: syntax OK + style fail × 2 → assert `style_bypass_active = True`, edge to Analyst, no further CoderAgent calls.
6. **Prompt Swap RBAC** — `test_rbac_matrix.py`: parameterised matrix of (role × tool); assert tool registry returns the exact whitelist from §3.2 for each role.
7. **FULL_SWARM E2E** — `test_full_swarm_e2e.py`: end-to-end mission ("add an `is_admin` flag to User model + migration + test"); assert WBS produces 3 steps with roles `core_dev`/`devops_infra`/`qa_tester`; assert `immutable_wbs` frozen on turn 1; assert DriftMonitor silent.
8. **Lifecycle Shutdown** — `test_lifecycle_manager.py`: simulate VS Code window-close event; assert MCTS subprocesses killed within 1 s; assert `wal_checkpoint(TRUNCATE)` executed; assert Ollama keep_alive flushed.
9. **Budget Override** — `test_finops_override.py`: set `max_budget_usd = 0.01`; queue a CLOUD step; assert HITL pause with `BUDGET_EXCEEDED`; resume after top-up.
10. **Strict typing & lint** — `mypy --strict ailienant-core` exit 0; `ruff check` exit 0.
11. **Regression** — full existing suite (260 tests at Phase 3 close) plus Phase 4 additions; target ≥ 320 passing tests, 0 regressions.

Manual smoke (after pytest green):
- Toggle Planner Mode in VS Code → confirm UI badge `Architect is writing SDD...`.
- Issue ambiguous prompt with no `.ailienant.json` → assert AnalystAgent enters Socratic dialogue (Phase 1.0.4 `Grill Me`).
- Force CSS<40 by removing GraphRAG cache → assert `is_red_alert` lights and run is routed to FULL_SWARM.

---

## 7. Roadmap Impact Analysis (Strategic Auditor checklist)

| Future Phase | Risk introduced by Phase 4 plan | Mitigation in this blueprint |
|---|---|---|
| `SCHEMA_EVOLUTION.MD` | `WBSStep.target_role` widens from 5 to 8 values (breaking literal). | Pydantic `model_validator(mode="before")` migrates legacy values during deserialization. Schema doc updated in same PR. |
| Phase 5 (MCP/Tool RAG) | Hardcoding tool whitelists in `roles.py` could clash with Tool RAG dynamic injection. | §3.2 whitelist is a **policy** layer; Tool RAG decides which of the *allowed* tools to *inject*. No conflict. |
| Phase 5.1 (Permission System) | `secops`/`devops_infra` HITL gates duplicate `PermissionMode` (`default`/`plan`/`auto`). | Phase 4 emits permission-level events only; the final allow/deny remains Phase 5's responsibility. |
| Phase 6 (DriftMonitor extensions) | Tighter drift thresholds may force Phase 4 Orchestrator changes. | DriftMonitor edge is already isolated; threshold lives in config, not in Orchestrator code. |
| Phase 7 (Frontend Toggle UI) | UI mode toggle must mirror `execution_mode`. | `execution_mode` already a top-level state field; WS contract change is mechanical. |

No blocking conflicts. No `SCHEMA_EVOLUTION.MD` contract violations (Phase 4 only adds fields, never removes/renames).
