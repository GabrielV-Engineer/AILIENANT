# SCHEMA_EVOLUTION.MD — Wire Contract History

> Contracts are **additive-only** and **version-tagged**. Consumers must tolerate unknown or missing fields. Each section is an append-only record of what was added in that phase. Fields are never deleted from this document — deprecated fields carry a `Deprecated:` annotation. Cross-reference `docs/PROJECT_MANIFEST.md` for the WBS context of each phase.

---

## 1. Core State Schema (`AIlienantGraphState`)

Initial Phase 0 specification. All fields below were present from first commit.

```python
from typing import TypedDict, List, Dict, Any, Optional, Annotated, Literal
from pydantic import BaseModel, Field
import operator

class ContextMeter(BaseModel):
    semantic_similarity: float  # 0.0 to 1.0
    graph_coverage: float       # 0.0 to 1.0
    recency_score: float        # 0.0 to 1.0
    css_total: float            # Context Sufficiency Score (0–100%)
    task_complexity_index: float  # TCI (0–100%)
    routing_decision: str       # "LOCAL_SMALL" | "LOCAL_BIG" | "CLOUD"
    is_red_alert: bool          # True when CSS < 40%

class LLMProfile(BaseModel):
    model_name: str            # e.g. "llama3:8b-instruct-q4_K_M"
    parameters_b: float        # e.g. 8.0, 1.5, 32.0 (logical capacity proxy)
    context_window: int        # e.g. 8192 (crash prevention)
    quantization: str          # e.g. "int4", "fp16"

class TokenCounter(BaseModel):
    local: int = 0
    cloud: int = 0
    total_cost_usd: float = 0.0

# --- Virtual File System (VFS) ---
class VFSFile(BaseModel):
    content: str = Field(..., description="Plain-text file contents")
    document_version_id: str = Field(..., description="Timestamp or hash for OCC")
    is_dirty: bool = Field(default=False, description="True if sync with IDE is pending")

class EnvironmentProfile(BaseModel):
    os_name: str
    vram_available_gb: float
    available_models: Dict[str, LLMProfile]  # "LOCAL_SMALL" | "CLOUD" → profile

# --- Spec-Driven Development (WBS) ---
class WBSStep(BaseModel):
    step_number: int
    target_role: Literal["Refactor", "Infra", "Doc", "SecOps", "Test"] = Field(
        description="Role the CoderAgent adopts for this step."
    )
    action: Literal["read_file", "write_file", "edit_file", "run_command"]
    target_file: str
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"

class MissionSpecification(BaseModel):
    """The macro-contract that bounds CoderAgent execution."""
    outcome: str
    scope: List[str]
    constraints: List[str]
    decisions: List[str]
    tasks: List[WBSStep]
    checks: List[str]

class AIlienantGraphState(TypedDict):
    # --- Base Memory & Request ---
    task_id: str
    user_input: str
    messages: Annotated[List[Dict[str, str]], operator.add]

    # --- Dynamic Telemetry ---
    context_metrics: ContextMeter
    active_llm_profile: LLMProfile
    token_usage: TokenCounter

    # --- Flow Control (Prompt Swapping) ---
    is_manual_override: bool
    target_role: Optional[str]        # Forced role when override is active
    current_step_id: Optional[int]    # ID of the currently executing WBS task

    # --- Immutable Planning (SDD) ---
    mission_spec: Optional[MissionSpecification]

    # --- VFS (Single Source of Truth) ---
    read_files_state: Dict[str, VFSFile]
    vfs_buffer: Dict[str, VFSFile]

    # --- Diagnostics ---
    errors: Annotated[List[str], operator.add]
    retry_count: int
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str

    # --- Planning Mode (DEPRECATED — see Phase 7.13.9 replacement) ---
    # planning_strategy: PlanningStrategy   # Deprecated — replaced by planner_mode_active
    # human_wbs_draft: Optional[str]        # Deprecated — replaced by hitl_response
    # wbs_approved: bool                    # Deprecated — replaced by shared_understanding_reached
```

> **Deprecated:** `planning_strategy`, `human_wbs_draft`, and `wbs_approved` were removed and replaced by the `planner_mode_active` / `hitl_pending` / `hitl_response` / `shared_understanding_reached` quartet documented in the Phase 7 State Expansion section below.

---

## 2. Mutation Rules (State Transition Permissions)

To guarantee predictable flow and avoid collisions in LangGraph, mutation permissions are strictly centralized:

- **ResearcherAgent (Retrieval Only):** Strict read-only access to the filesystem. The only agent authorized to mutate context by injecting `researcher_skeleton`. No permission to alter source code.
- **PlannerAgent:** The only agent authorized to initialize and mutate `mission_spec`. While running (O(1)), the graph enters "plan mode", globally blocking all write tools.
- **OrchestratorAgent:** The only agent with authority to manage the execution cycle (O(N)). Mutates routing metrics. Has absolute authority to block the graph and inject `HITL_APPROVAL_REQUIRED` state when execution attempts to deviate from `mission_spec`. No write permissions for code.
- **CoderAgent (The Mutant Worker):** The **ONLY node** with Write and Execute permissions. Its mutation permissions change dynamically at runtime based on the Role injected by the Orchestrator (see `agents/roles.py`).
- **AnalystAgent (The Ant):** Immutable operation (read-only). Mutates the UI conversational state, never source code. Uses ReadOnly tools crossing short-term memory (`AIlienantGraphState`), long-term memory (GraphRAG), and IDE dirty buffers.

---

## 3. Persistence and State Consolidation

The LangGraph SQLite checkpointer is the underlying persistence engine.

- **Consolidation Rule (Commit):** The transient graph state (`AIlienantGraphState`) does not persist automatically. Every confirmed code mutation executed by the CoderAgent must pass the memory engine's hash validation.
- **Synchronization:** Only after a successful exit code (from the testing micro-swarm) and after passing hash validation does code permanently reflect in long-term memory (LanceDB / NetworkX), updating the context for future ResearcherAgent retrievals.

---

## 4. ContractGuard Fields (Additive Schema Growth)

`AIlienantGraphState` gains two optional fields. `ContextMeter` Pydantic model remains **immutable**.

| Field | Type | Owner (mutation) | Semantics |
|---|---|---|---|
| `ui_payload` | `Optional[Dict[str, object]]` | `ContractGuardNode` | Render signal for the VS Code extension. When non-None with `action="RENDER_PERSISTENT_CONTRACT"`, the frontend draws the persistent banner then acks it (sets back to `None`). |
| `contract_anchor` | `Optional[Dict[str, object]]` | `ContractGuardNode` (only) | Snapshot of `{tci, target_role, turn}` at the last emission. Read-only for the rest of the graph. `None` until first emission. |

Both are scalar overwrite (no reducer) — not applied in parallel fan-out because `ContractGuardNode` runs serially after `coder_agent`.

**Deterministic invariants (O(1)):**

- **TCI Delta:** `abs(state["tci"] - anchor["tci"]) > 15.0` (absolute points on 0–100). Requires a prior anchor.
- **CSS at Token Capacity:** `state["css"] < 40.0` AND `(token_usage.local + token_usage.cloud) / active_llm_profile.context_window >= 0.80`. The only rule that can fire on the first turn (no anchor needed).
- **Subgraph/Domain Shift:** `state["target_role"] != anchor["target_role"]` (and `target_role is not None`). Requires a prior anchor.

**Payload contract:**

```jsonc
{
  "action": "RENDER_PERSISTENT_CONTRACT",
  "reason": "TCI_DELTA | CSS_AT_CAPACITY | SUBGRAPH_SHIFT",
  "contract": {
    "mission_outcome": "...",
    "active_role": "Refactor|Infra|Doc|SecOps|Test",
    "in_scope": ["..."],
    "out_of_scope": ["..."],
    "open_constraints": ["..."],
    "trigger_reason": "TCI_DELTA | CSS_AT_CAPACITY | SUBGRAPH_SHIFT"
  }
}
```

`SessionContract` (Pydantic) is the binding contract for the LLM and the extension. Any network, parse, or LLM validation failure falls back to a deterministic skeleton built from `mission_spec.outcome / .scope / .constraints` — the banner always renders if the trigger fired.

---

## 5. Resource Broker Fields (Additive Schema Growth)

Three new optional fields in `AIlienantGraphState`. `ContextMeter` Pydantic model remains **immutable**.

| Field | Type | Owner (mutation) | Semantics |
|---|---|---|---|
| `ui_interrupt` | `Optional[Dict[str, object]]` | `ResourceBroker` | Blocking modal signal. **Distinct** from `ui_payload` (persistent banner). Cleared by the extension when the user selects a resolution. |
| `contention_status` | `Optional[Dict[str, object]]` | `ResourceBroker` (only) | Telemetry at contention time: `requested_model`, `holder_session`, `queue_len`, `recommendation`. |
| `user_resource_resolution` | `Optional[Literal["WAIT","SWITCH_TO_CLOUD","CANCEL"]]` | `ResourceBroker` (reads from HITL reply) | User response consumed inside the same node. |

All three are scalar overwrite (no reducer) — the broker runs serially from its protected call sites.

**Recommendation heuristic (`_compute_recommendation`):**

- `TCI > 75` → `SWITCH_TO_CLOUD` (heavy task, cloud is faster)
- `TCI < 40` → `SWITCH_TO_CLOUD` (light task, no point waiting)
- `40 ≤ TCI ≤ 75` + empty queue → `WAIT`
- `40 ≤ TCI ≤ 75` + occupied queue → `SWITCH_TO_CLOUD`

**`ui_interrupt` contract:**

```jsonc
{
  "action": "RESOURCE_CONTENTION_INTERRUPT",
  "payload": {
    "conflicting_model": "ailienant/small",
    "task_tci": 55.0,
    "recommendation": "WAIT | SWITCH_TO_CLOUD",
    "queue_position": 1,
    "estimated_wait_seconds": 20
  }
}
```

**WebSocket transport (no changes to `ws_contracts.py`):**

The broker reuses `vfs_manager.request_human_approval(...)` (same pattern as `drift_monitor` and `finops_gate`). Distinguished via sentinel:
- `action_description == "RESOURCE_CONTENTION"` → the extension deserializes `proposed_content` as JSON to obtain the rich payload.
- User response in `client_hitl_response` with `comment ∈ {"WAIT","SWITCH_TO_CLOUD","CANCEL"}`. The `approved` field is ignored for this action.

**Anti-deadlock discipline (CRITICAL):**

Every call site (planner, summarizer, mcts_coder) MUST wrap the entire lock-held region — not just the `LLMGateway.ainvoke` call, but also post-LLM parsing and Pydantic validation — in a `try/finally` that invokes `await ResourceBroker.release(session_id)` if `decision.holds_lock`. A JSON parsing error that escapes without releasing the lock would permanently block all other sessions. Regression covered by `test_lock_released_when_post_llm_processing_raises`.

---

## 6. Permission Engine + Cognitive Quarantine Channels

**Strictly additive. No removals, no renames, no Literal narrowing.** Existing checkpoints deserialize unchanged because every new field has a safe default at construction sites.

**New enums in `core/permissions.py`:**

- `SessionPermissionMode` — `{DEFAULT, PLAN, AUTO}` — per-mission HITL policy.
- `ToolPrivilegeTier` — `{READ_ONLY, WRITE, EXECUTE, DANGEROUS}` — per-tool tier declared at registration.
- `PermissionDecision` — `{ALLOW, HITL, DENY}` — outcome of the pure `evaluate_action()` matrix.
- `PermissionDeniedError(Exception)` — raised by `rbwe_guard`, surfaced to agent scratchpad (not crash).

**`AIlienantGraphState` extended (8 new channels, end of TypedDict body):**

| Field | Type | Reducer |
|---|---|---|
| `session_permission_mode` | `Literal["DEFAULT","PLAN","AUTO"]` | last-write |
| `boundary_id` | `Optional[str]` | last-write (rotates each turn) |
| `tool_registry_active` | `List[str]` | last-write |
| `permission_audit_log` | `Annotated[List[Dict[str, Any]], operator.add]` | append-only |
| `pending_hitl_request` | `Optional[Dict[str, Any]]` | last-write (cleared on response) |
| `background_tasks` | `Dict[str, Dict[str, Any]]` | last-write per key |
| `mcp_server_endpoint` | `Optional[str]` | last-write |
| `rbwe_violations` | `Annotated[List[str], operator.add]` | append-only |

Orthogonal to existing `shared/rbac.py::PermissionMode` (`{PLAN_ONLY, ROUTING_ONLY, EDIT_EXECUTE_RBW, READ_ONLY}`) — `shared/rbac.py` unchanged.

---

## 7. Tool RAG + MCP Transport

**Strictly additive. No state-field renames or removals; no enum widening; no breaking change to `mcp_adapter` public API.**

**New constants in `core/tool_rag.py`:**

- `TOOL_RAG_TOP_K = 5` — hard cap on schemas returned per `select_tools()` call.
- `TOOL_RAG_MIN_REDUCTION = 0.70` — mean prompt-size reduction target (asserted by Phase 5.7 gate).
- `MCP_HANDSHAKE_TIMEOUT_SEC = 5.0` — `asyncio.wait_for` deadline for `bootstrap_mcp_session()`.

**New classes/functions:**

- `ToolSchema` (frozen dataclass) — in-memory representation `{name, description, json_schema, privilege_tier, allowed_roles (frozenset[str]), embedding (List[float] | None)}`. On disk in LanceDB, `allowed_roles` is persisted as a comma-flanked string for future `.where()` filtering.
- `ToolRAGStore` — RAM-resident (tempfile-backed) LanceDB store, schemas only. Methods: `register_schema()`, `select_tools()`, `all_schemas()`, `clear()`, `prompt_size_metrics()`.
- `core/tool_rag.tool_rag_store` — module-level singleton.
- `tools/mcp_adapter.bootstrap_mcp_session(uri, state, *, timeout_sec)` — opens stdio MCP session, harvests `session.list_tools()`, populates Tool RAG store + `core.db.tool_registry` SQLite table. Returns `bool`; never raises.
- `brain/swarms.tool_rag_select_node(state)` — graph node spliced before `coder_agent` in MICRO_SWARM (also covers FULL_SWARM via embedded sub-graph).

**New env var:**

- `AILIENANT_MCP_SERVER_URI` — default `None` → local-only fallback. Format: `stdio:///absolute/path/to/server[?arg=val]`.

**`AIlienantGraphState` channels (NO new fields, only consumption changes):**

- `tool_registry_active` — now written by `tool_rag_select_node` per turn.
- `mcp_server_endpoint` — written by `bootstrap_mcp_session` at app start.
- `permission_audit_log` — appended by both above.

Existing checkpoints deserialize unchanged.

---

## 8. Perception/Mutation/Execution/Control Tools + Adversarial Gate

**Strictly additive at the schema level.** No new `AIlienantGraphState` channels. No `core/permissions.py` edits. No new dependencies beyond `pypdf==5.4.0` and `markdownify==1.1.0`.

**Registered tool schemas (14 total):**
- Perception (5): `document_parser`, `inspect_ast_node`, `get_symbol_references`, `trace_data_flow`, `web_fetch`
- Mutation (3): `atomic_code_patch`, `batch_semantic_edit`, `file_write`
- Execution (4): `sandbox_bash`, `task_create`, `task_get`, `check_type_integrity`
- Control (2): `ask_user_question`, `toggle_plan_mode`

`TOOL_RAG_MIN_REDUCTION = 0.70` empirically validated: eager 6054 bytes, selected 1810 bytes → ratio 0.7010.

Known tech debt: `permission_audit_log` and `rbwe_violations` (`operator.add`) grow O(N) over long missions; reducer swap to truncating `(old + new)[-cap:]` deferred.

---

## 9. OOM Cascade, DLQ, FinOps Supervisor, HITL Audit Ledger

**Additive only. No renames, no removals, no Literal narrowing. Phase 5.7 checkpoints deserialize unchanged.**

### 9.1 - 6 Operational-Safety State Channels

`brain/state.py::AIlienantGraphState` gains 6 channels — all scalar overwrite, safe defaults:

| Field | Type | Owner |
|---|---|---|
| `accumulated_session_cost` | `float` | `core/supervisor.py` |
| `session_max_budget_usd` | `float` | task_service (injected at graph construction) |
| `oom_fallback_active` | `bool` | `tools/llm_gateway.py` on OOM trap |
| `sandbox_tier_active` | `Literal["DOCKER","WASM","NATIVE_HITL"]` | graph factory |
| `hitl_audit_chain_head` | `Optional[str]` | `core/audit.py` |
| `dead_letter_episode_id` | `Optional[str]` | `main.py::resume_task` |

### 9.2 — `dead_letter_tasks` Table

New SQLite table in the catalog DB (`DB_CATALOG_PATH`):

```sql
CREATE TABLE IF NOT EXISTS dead_letter_tasks (
    episode_id               TEXT    PRIMARY KEY,   -- uuid4 hex
    task_id                  TEXT    NOT NULL,
    thread_id                TEXT    NOT NULL,
    failed_node              TEXT    NOT NULL,
    exception_class          TEXT    NOT NULL,
    exception_message        TEXT    NOT NULL,      -- truncated to 2000 chars
    state_snapshot_blob_hash TEXT    NOT NULL,      -- blake2b CAS ref
    created_at               INTEGER NOT NULL,
    resolved_at              INTEGER                -- NULL until successful resume
);
CREATE INDEX IF NOT EXISTS idx_dlq_task_id ON dead_letter_tasks(task_id, created_at);
```

`resolved_at` enables idempotent resume: a repeated `POST /api/v1/task/resume/{task_id}` is a no-op once stamped.

### 9.3 — FinOps Supervisor

No new state channels, no new tables. `core/supervisor.py` consumes channels. `SESSION_BUDGET_HARD_KILL` is appended to the existing `security_flags` channel.

### 9.4 — `hitl_audit_log` Table

New SQLite table in the catalog DB:

```sql
CREATE TABLE IF NOT EXISTS hitl_audit_log (
    audit_id                  TEXT    PRIMARY KEY,   -- uuid4 (== approval_id)
    session_id                TEXT    NOT NULL,
    request_kind              TEXT    NOT NULL,
    action_description        TEXT    NOT NULL,
    proposed_content_scrubbed TEXT,                  -- secrets-redacted
    proposed_content_hash     TEXT    NOT NULL,      -- blake2b(scrubbed)
    resolution                TEXT    NOT NULL,      -- approved | rejected | timeout
    resolution_comment        TEXT,
    operator_user_email       TEXT,
    prev_chain_hash           TEXT,                  -- NULL for session genesis row
    chain_hash                TEXT    NOT NULL,
    resolved_at               INTEGER NOT NULL
);
```

**Chain formula:** `chain_hash = blake2b(prev_chain_hash ‖ audit_id ‖ session_id ‖ request_kind ‖ action_description ‖ proposed_content_hash ‖ resolution ‖ resolved_at).hexdigest()`.

### 9.5 — Secrets Scrubber + OOM Telemetry Table

`SecretsScrubberFilter` process-wide logging filter — no schema change.

adds one table to the **telemetry DB** (`data/telemetry.sqlite`, NOT the catalog DB):

```sql
CREATE TABLE IF NOT EXISTS oom_fallback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id        TEXT,
    event             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    original_model    TEXT NOT NULL,
    fallback_model    TEXT NOT NULL,
    tokens_at_failure INTEGER,
    swap_latency_ms   REAL
);
```

no schema change — adds read-only `GET /api/v1/dlq/pending` endpoint.

---

## 10. GraphRAG Enrichment: Additive Columns on `dependency_graph` + `ppr_scores`

**Additive only. Three nullable columns added to existing catalog-DB tables; no `AIlienantGraphState` channel changes.** Same idempotent `PRAGMA table_info`-guarded `ALTER TABLE ADD COLUMN` path.

- **`dependency_graph.confidence TEXT`** + **`dependency_graph.confidence_score REAL`** — per-edge confidence. Taxonomy: `EXTRACTED` (1.0) = edge target is an indexed source file; `AMBIGUOUS` (0.25) = module stem collides with ≥2 indexed files; `INFERRED` (0.5) = everything else.
- **`ppr_scores.leiden_community_id INTEGER`** — node community id from networkx Louvain (deterministic `seed=42`). Named `leiden_*` for forward-compatibility; algorithm is Louvain. `NULL` until the analytics pass runs.

---

## 11. Reactive Indexing Idempotency: Additive `indexed_files.content_hash`

**Additive only. One nullable column on `indexed_files`; no channel changes.**

- **`indexed_files.content_hash TEXT`** — `sha256` of the last reactively-indexed file revision. A byte-identical re-save costs nothing (content_hash match skips AST extraction and re-embedding). The bulk indexer inserts `NULL`; the first reactive save backfills.

---

## 12. Native Thinking: Additive WS Event + `TaskPayload` Fields

**Additive only. No `AIlienantGraphState` channel changes.**

- **New WS event** `ServerThinkingChunkEvent` (`api/ws_contracts.py`), discriminator `event_type="server_thinking_chunk"`, payload `ThinkingChunkPayload{ session_id: str, delta: str, token_count: int = 0 }`.
- **`TaskPayload` growth** (`core/task_service.py`): `enable_native_thinking: bool = True`, `thinking_budget_tokens: int = 4096`. Omitted field from a client keeps prior behavior.
- **No persisted/graph schema:** reasoning text is transport-only — not a graph-state channel, not written to the checkpointer, excluded from `PERSIST_TRANSCRIPT`.

---

## 13. State Expansion

Approximately 40 channels were added incrementally. They are grouped below by functional cluster. All are scalar overwrite unless a reducer is noted.

### 13.1 Workspace / Project Identity

| Field | Type | Owner |
|---|---|---|
| `project_id` | `Optional[str]` | `client_workspace_init` payload |
| `workspace_root` | `Optional[str]` | `client_workspace_init` payload |
| `explicit_mentions` | `List[str]` | Payload parser (`@mention` extraction) |
| `attachments` | `List[ManualAttachment]` | Payload parser (manual file attachments) |
| `workspace_pid` | `Optional[int]` | `WorkspaceLifecycleManager` |
| `workspace_active` | `bool` | `WorkspaceLifecycleManager` |
| `venv_interpreter_path` | `Optional[str]` | Workspace discovery (venv probe) |

### 13.2 Routing Refinements

| Field | Type | Notes |
|---|---|---|
| `tci` | `float` | Scalar mirror of `ContextMeter.task_complexity_index` — avoids Pydantic deserialization in hot routing path |
| `css` | `float` | Scalar mirror of `ContextMeter.css_total` |
| `has_images` | `bool` | Vision bypass gate: skips text routing when True |
| `routing_warning` | `Optional[str]` | Non-fatal advisory emitted by routing engine |
| `hardware_profile` | `Optional[HardwareProfile]` | Hardware profiler snapshot |
| `provider` | `str` | Active litellm provider key (e.g. `"anthropic"`, `"ollama"`) |
| `execution_mode` | `Literal["SEQUENTIAL","MICRO_SWARM","FULL_SWARM"]` | Orchestrator-assigned LangGraph topology |
| `active_role` | `Optional[Literal["core_dev","architect_refactor","devops_infra","secops","qa_tester","doc_manager","vcs_manager","data_ml_engineer"]]` | Orchestrator-assigned coder role |

### 13.3 Planner UI + HITL Channels

> Replaces the deprecated `planning_strategy` / `human_wbs_draft` / `wbs_approved` triad.

| Field | Type | Notes |
|---|---|---|
| `planner_mode_active` | `bool` | Socratic planning session is active |
| `hitl_pending` | `bool` | Turn suspended — awaiting user plan approval |
| `hitl_response` | `Optional[str]` | User's review response text |
| `shared_understanding_reached` | `bool` | Transition flag that closes the planner session |

### 13.4 Coder Output Pipeline

| Field | Type | Reducer |
|---|---|---|
| `generated_code` | `Dict[str, VFSFile]` | `_merge_generated_code` |
| `pending_patches` | `Dict[str, str]` | `operator.or_` |
| `pending_contents` | `Dict[str, str]` | `operator.or_` (filepath → full replacement content) |
| `pending_base_hash` | `Dict[str, str]` | `operator.or_` (filepath → sha256 of pre-edit content, EOL-normalized) |
| `researcher_skeleton` | `Optional[str]` | Researcher → Planner handoff channel |
| `session_delta` | `str` | Token streaming accumulator for in-progress turns |

### 13.5 Syntax / Style Validation Gate

| Field | Type |
|---|---|
| `relaxed_typing_mode` | `bool` |
| `style_bypass_active` | `bool` |
| `consecutive_style_failures` | `int` |
| `syntax_gate_status` | `Literal["pass","fail","pending"]` |
| `code_under_validation` | `Optional[str]` |
| `style_gate_status` | `Literal["pass","fail","pending"]` |
| `is_indexing_complete` | `bool` |
| `guardrail_failed` | `bool` |
| `validation_feedback` | `Optional[str]` |

### 13.6 Circuit Breaker / Retry

| Field | Type | Notes |
|---|---|---|
| `error_streak` | `int` | Consecutive coder failures without recovery |
| `circuit_breaker_tripped` | `bool` | Halts coder dispatch when True |
| `cloud_surgeon_invocations` | `int` | `operator.add` — cloud escalation counter |
| `planner_retry_count` | `int` | Planner planning-loop retries |

### 13.7 Error Correction / Self-Healing

| Field | Type |
|---|---|
| `healing_required` | `bool` |
| `correction_attempts` | `int` |
| `last_error_trace` | `Optional[str]` |
| `failed_node` | `Optional[str]` |
| `failure_signature` | `Optional[str]` |
| `termination_reason` | `Optional[str]` — set to `"user_abort"` on `asyncio.CancelledError`; cold-serializable; UI renders "Stopped by user" on rehydrate |

### 13.8 Agentic Cell

| Field | Type | Reducer |
|---|---|---|
| `agentic_iteration` | `int` | (none) |
| `agentic_trajectory` | `List[Dict[str, Any]]` | `operator.add` |

### 13.9 Per-Task Budget (distinct from Phase 6.3 session-level fields)

| Field | Type | Reducer | Notes |
|---|---|---|---|
| `current_cost_usd` | `float` | `operator.add` | Counts only the **current task turn** |
| `max_budget_usd` | `float` | (none) | Per-task hard ceiling |

> **Distinction:** `current_cost_usd` / `max_budget_usd` are per-task counters reset each task dispatch. `accumulated_session_cost` / `session_max_budget_usd` span the entire WebSocket session and are never reset mid-session.

---

## 14. `agent_todos` Channel (Universal Tools)

**Additive only. One new `AIlienantGraphState` channel; no renames, no removals. Older checkpoints rehydrate unchanged; readers use `.get("agent_todos") or []`.**

```python
agent_todos: Annotated[List[Dict[str, Any]], _merge_todos]
```

- **Added in:** `brain/state.py`, alongside reducer `_merge_todos`.
- **Item shape:** `{content: str, status: "pending"|"in_progress"|"completed", active_form: str}` — the same structure `TodoWriteTool._arun` emits and `TodoWriteInput` validates.
- **Reducer semantics (`_merge_todos`):**
  - `right is not None` → return `right` (including `[]`, which explicitly clears the list).
  - `right is None` → return `left or []` (no-op for turns that do not call `todo_write`).
  - Tests `is not None`, never truthiness, so an empty list is a valid "clear" signal — the anti-immortal-TODO invariant.
- **No runtime call site yet:** `todo_write._arun` returns a JSON string keyed `agent_todos`; a future graph node will `json.loads` it and write the delta through this channel. Registration-surface only as of Division 8.8.

## 14. Skills, MCP Config, Hooks, Agent Role Override Tables

**No `AIlienantGraphState` channel changes.** Four new SQLite catalog tables:

```sql
CREATE TABLE IF NOT EXISTS skills (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    body         TEXT NOT NULL,
    description  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    scope        TEXT NOT NULL DEFAULT 'global',  -- 'global' | 'workspace'
    workspace_root TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    transport  TEXT NOT NULL DEFAULT 'stdio',
    uri        TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hooks (
    id         TEXT PRIMARY KEY,
    event      TEXT NOT NULL,
    command    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_role_overrides (
    role          TEXT PRIMARY KEY,
    system_prompt TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
```

`mcp_server_endpoint` (Phase 5.1 channel) is now written by the MCP auto-connect bootstrap during the FastAPI lifespan rather than only by manual calls.

**Curated MCP Registry (`core/mcp_registry.py`):**

| Server | Privilege map |
|---|---|
| `github` | `create_pull_request` → WRITE · `merge_pull_request` → DANGEROUS |
| `brave-search` | `search` → READ_ONLY |
| `docker` | `run` → EXECUTE |
| `postgres` | `query` → READ_ONLY · `execute` → EXECUTE |

---

## 15. External Gateway Catalog

Public surface exposed via stdio MCP server (`gateway/catalog.py`). All input schemas use strict `type: "object"` with `"additionalProperties": false`.

| Capability | Privilege tier | Mode | Notes |
|---|---|---|---|
| `run_task` | EXECUTE | async | Submits a coding task; returns `task_id`. Spawned task runs under `INTERNAL_TASK_MODE = DEFAULT` — never AUTO. |
| `run_benchmark` | EXECUTE | async | Triggers benchmark harness; returns `task_id`. |
| `check_task_status` | READ_ONLY | sync | Polls `task_id` status from in-memory + DLQ. |
| `query_memory` | READ_ONLY | sync | GraphRAG semantic search. |
| `get_dependents` | READ_ONLY | sync | 1-hop backward symbol lookup. |
| `get_workspace_graph` | READ_ONLY | sync | Snapshot of the code-dependency graph. |
| `get_report` | READ_ONLY | sync | Reads a completed benchmark artifact. |

**Governance constants (`gateway/governance.py`):**

- `_INVOCATION_GATE_MODE = SessionPermissionMode.AUTO` — pre-authorizes READ_ONLY/EXECUTE verbs; routes DANGEROUS to HITL.
- `INTERNAL_TASK_MODE = SessionPermissionMode.DEFAULT` — conservative; a spawned task cannot self-escalate its permissions.
- `_ESCALATION_KEYS = ("mode", "execution_mode", "session_permission_mode", "permission_mode")` — any caller-supplied key in this list is stripped from the payload (caller cannot escalate privileges via injection).

---

## 16. Analyst Tri-Brain Context Protocol

`ContextChunk` dataclass and `ContextBudgetManager.pack()` contracts (`agents/analyst_context.py`):

**`ContextChunk` fields:**
- `body: str` — content text
- `brain: str` — source identifier: `"codex"` | `"file"` | `"graphrag"` | `"docs"` | `"readme"`
- `label: str` — human-readable label for drop reporting
- `tokens: int` — measured by `PrecisionTokenCounter`

**Budget tiers (Dict[str, int]):**

| Tier | Tokens |
|---|---|
| `"small"` | 1500 |
| `"medium"` | 3000 |
| `"big"` | 6000 |
| `"cloud"` | 8000 |

**Retention ladder (lower priority number = kept first under budget pressure):**

| Brain source | Priority |
|---|---|
| `codex` | 0 (pinned) |
| `file` | 1 (pinned, capped to 60% of budget) |
| `graphrag` | 2 |
| `docs` | 3 |
| `readme` | 4 |

**Character pre-caps (applied before token counting):**
`CODEX_CAP = 1024` · `FILE_CAP = 4096` · `RAG_CAP = 2048` · `WS_CAP = 1024` · `README_CAP = 3072`

**`pack()` contract:**
- Whole-chunk-only packing (never mid-chunk split).
- Returns `(packed_context_text: str, dropped_labels: List[str])`.
- Hard cap: 60% of budget reserved for `file` brain; remaining 40% split by ladder priority.

---

## 17. WebSocket Contract Additions

The following event types were added to `api/ws_contracts.py` after Phase 6.9. The full union now contains 60+ events. Grouped by functional area:

**Inline edit pipeline:**
`client_inline_edit_request` · `client_inline_edit_cancel` · `server_inline_edit_start` · `server_inline_edit_delta` · `server_inline_edit_end`

**Abort mesh (ADR-706):**
`client_abort_mesh` · `server_abort_ack`

**Tool chips / tool transparency:**
`server_tool_start` · `server_tool_stream_chunk` · `server_tool_result` · `server_tool_dep_graph` · `client_retry_tool` · `client_invoke_tracked_bash`

**Planner surface:**
`client_planner_mode_toggle` · `server_plan_document`

**VFS / OCC patch flow:**
`server_vfs_patch_approved` · `server_apply_workspace_edit` · `client_patch_applied`

**Analyst / Natt chat channel:**
`client_analyst_query` · `server_natt_message` · `server_natt_token` · `server_natt_stream_end`

**Workspace lifecycle:**
`client_workspace_init` · `server_indexing_progress` · `server_indexing_error` · `server_byom_config_applied`

**IDE telemetry + Manual Dreaming:**
`client_ide_telemetry` · `client_dreaming_run`

**HITL ACK:**
`server_hitl_ack`

**Checkpointing / time travel:**
`client_branch_from_checkpoint` · `server_session_branched`

**PTY / Agentic Cell:**
`client_pty_write` · `server_cell_tool_start` · `server_cell_pty_chunk` · `server_cell_ast_diff` · `server_cell_governor_tick`

**Native Thinking:**
`server_thinking_chunk`

**Context pipeline:**
`state_compacted` (see §25)

**Devcontainer host bridge:**
`server_devcontainer_provision_request` · `client_devcontainer_provision_status` · `server_devcontainer_exec_request` · `client_devcontainer_exec_stream` · `client_devcontainer_exec_exit` (see §26)

---

## §18 — WBSStep Dependency Graph Field

**Field:** `WBSStep.depends_on: Optional[List[int]] = None`
**Location:** `ailienant-core/brain/state.py` — `WBSStep` Pydantic model
**Schema version:** additive (no version bump required; default `None` is backward-compatible with all existing checkpoints)

**Contract:**
- When present, each integer must reference a `step_number` that exists in the same `MissionSpecification.tasks` list.
- The directed graph formed by `depends_on` links must be acyclic (a DAG).
- `ValidateWBSDependenciesTool` (Pass 5, Kahn's BFS) enforces both constraints before the plan is committed; violations produce blocking issue types `"dependency_cycle"` or `"invalid_depends_on"`.
- Consumers that do not read `depends_on` are unaffected — the field is optional and defaults to `None`.

---

## §19 — Routing Signal Producer: Researcher Node

**Change:** the cognitive routing signal is now **emitted by the Researcher node**, not the Planner.
**Location:** producer moved from `ailienant-core/agents/planner.py` to `ailienant-core/agents/researcher.py`.
**Schema version:** additive (no field shape change) — every channel below already existed; only the producing node changed. The `researcher_agent` graph node is spliced before `planner_agent`.

**Channels the Researcher emits (Planner + downstream consume):**
- `context_metrics: ContextMeter` — carries `semantic_similarity`, `graph_coverage`, `recency_score`, `css_total`, `task_complexity_index`, `routing_decision` (a field on this model: `"CLOUD" | "LOCAL_SMALL" | "LOCAL_BIG"`), `is_red_alert`.
- `css: float`, `tci: float` — top-level mirrors of `css_total` / `task_complexity_index`.
- `provider: str` — `"CLOUD" | "LOCAL"`, consumed by `route_to_coders` (SWARM/RELAY) and coder model selection.
- `routing_warning: Optional[str]` — a **distinct** channel, non-None only on hardware (VRAM-floor) reroute. Not a synonym for `routing_decision`.
- `researcher_skeleton: str` — the dense AST skeleton the Planner consumes in place of the deleted deep-context prompt block.

**Contract:**
- The Researcher owns all retrieval (GraphRAG, fast-boot, @-mention bypass), recency, the Context Meter Cascade (CSS → red-alert → mini-judge → routing), and the hardware reroute. The Planner is a pure analytical WBS engine that consumes the resolved signal.
- The Researcher always emits a non-None `context_metrics` (including on its error path); the Planner retains a defensive cold-default init so a Researcher bypass can never propagate `None` into routing.
- `routing_decision` is read via `context_metrics.routing_decision` — never as a top-level key.

---

## §20 — HITL Transport: In-Graph Approvals via LangGraph Suspend & Resume

**Change:** in-graph human approvals now ride LangGraph's native `interrupt()` / `Command(resume=…)` channel instead of an inline `asyncio.Event` (`request_human_approval`).
**Location:** `ailienant-core/core/hitl.py` (`request_graph_approval`, `extract_pending_interrupt`); consumed by `brain/finops.py`, `brain/drift_monitor.py` (`drift_gate`), `brain/agentic_cell.py`; resumed by `core/task_service.py::resume_graph` + the `main.py` WS handler.
**Schema version:** additive. The `ServerHITLApprovalRequestEvent` / `client_hitl_response` wire contracts are unchanged; the `approval_id` is cosmetic (card correlation), never the resume key.

**Contract:**
- A paused graph is detected from state (`aget_state(cfg).interrupts` / non-empty `.next`) after `astream` ends naturally — LangGraph swallows the `GraphInterrupt`, so a pause is never observed as a raised exception or a graph failure.
- Resume correlation is LangGraph-native (positional within the task); the resume value is `Command(resume={"approved": bool, "comment": str|None})`.
- New `AIlienantGraphState` channels (additive, last-value): `drift_gate_open: Optional[bool]`, `drift_similarity: Optional[float]` (committed by `drift_compute`, read by `drift_gate`); `pending_exec_command: Optional[str]` (the agentic cell's deferred HITL-gated command).
- Non-graph HITL (MCP adapter, post-graph file-write apply loop) stays on the `request_human_approval` event channel — `interrupt()` only works inside a running graph node.
- A node that interrupts commits no pre-interrupt writes; the interrupt-decision must be produced by a prior committed node (drift split) and no non-idempotent side effect may precede the interrupt (cell defer → interrupt-first exec phase).

---

## §21 — Corpus-Presence Probe: Additive `corpus_empty` Param to `derive_routing_decision`

**Change:** `derive_routing_decision` gains one new optional parameter `corpus_empty: bool = False`.
**Location:** `ailienant-core/core/memory/context_auditor.py`
**Schema version:** additive, backward-compatible — default `False` preserves the original behavior for all existing callers.

**Semantics:**
- When `corpus_empty=True` the `css < 40.0 → CLOUD` red-alert floor is **skipped**; routing falls through to the TCI bands.
- When `corpus_empty=False` (default) behavior is identical to the pre-change signature: `css < 40.0` still forces CLOUD.
- The `fast_track` short-circuit is evaluated before `corpus_empty`; a fast-track call is unaffected.

**Produced by:** Researcher node (`agents/researcher.py`) via `await SemanticMemoryManager.is_corpus_empty(workspace_hash)`.
**New method:** `SemanticMemoryManager.is_corpus_empty(workspace_hash: str) -> bool` — short-TTL module-level cached (`_CORPUS_PRESENCE_TTL_S = 30.0`), invalidated on every corpus write (`_write_record`, `_delete_record`).
**ContextMeter.is_red_alert:** now computed as `(css < 40.0) and not corpus_empty` — semantically unchanged for non-empty workspaces; always `False` on an empty corpus regardless of CSS, so telemetry stays truthful (CSS is not altered).

---

## §22 — 7-Mode Session Permission Vocabulary (Additive `SessionPermissionMode` Members)

**Change:** `SessionPermissionMode` grows from 3 members to a canonical 7-mode vocabulary; the 3 original members are retained as deprecated aliases. The `session_permission_mode` state channel Literal widens to the union of both.
**Location:** `ailienant-core/core/permissions.py` (`SessionPermissionMode`, `_LEGACY_MODE_MIGRATION`); `ailienant-core/brain/state.py` (`AIlienantGraphState.session_permission_mode`).
**Schema version:** additive, backward-compatible. No member is removed or repurposed; every persisted checkpoint string (`"DEFAULT" | "PLAN" | "AUTO"`) remains a valid value. This change is **behavior-inert** — no runtime path emits a canonical member yet; the resolver matrix that consumes them lands separately, and the UI that selects them lands after that.

**Canonical members** (severity-ordered, permissive → restrictive):

| # | Member | Value | Behavior (resolved by the matrix) |
|---|--------|-------|-----------------------------------|
| 1 | `FULL_AUTO` | `full_auto` | No HITL for any tier; all tools execute immediately. |
| 2 | `STANDARD` | `standard` | HITL for DANGEROUS only. |
| 3 | `CAUTIOUS` | `cautious` | HITL for EXECUTE + DANGEROUS; READ_ONLY auto-admitted. |
| 4 | `ASK_EXECUTE` | `ask_execute` | Ask before any non-READ_ONLY tool; deny DANGEROUS. |
| 5 | `ASK_ALL` | `ask_all` | Ask before every tool call, including READ_ONLY. |
| 6 | `READ_ONLY` | `read_only` | Only READ_ONLY tier admitted; EXECUTE/DANGEROUS blocked. |
| 7 | `PLAN_ONLY` | `plan_only` | Planning only; no execution. |

**Deprecated legacy members** — retained for checkpoint back-compat, resolved onto the canonical vocabulary by `_LEGACY_MODE_MIGRATION`. The mapping is **behavior-faithful**: each target preserves the legacy mode's real gating behavior, never a silent privilege change on resume.

| Legacy member | Real legacy behavior | Canonical target |
|---------------|----------------------|------------------|
| `DEFAULT` — `Deprecated:` | HITL on WRITE + EXECUTE + DANGEROUS | `CAUTIOUS` (3) |
| `AUTO` — `Deprecated:` | HITL on DANGEROUS only | `STANDARD` (2) |
| `PLAN` — `Deprecated:` | Blocks all non-READ_ONLY | `PLAN_ONLY` (7) |

There is no legacy `READ_ONLY` *session* mode — `READ_ONLY` (6) is a new canonical member with no legacy source. (The pre-existing `READ_ONLY` is a `ToolPrivilegeTier`, an orthogonal axis.)

**Consumers (unchanged this revision):** `evaluate_action`, `gate_execute_action`, `rbwe_guard`, `session_mode_from_channel`, `session_mode_from_frontend` all continue to operate on the legacy members exactly as before. The canonical-native resolver rewrite and the migration enforcement (routing the legacy fallback through `_LEGACY_MODE_MIGRATION`) are a separate, later revision.

---

## §23 — 7×3 Permission Resolver: `evaluate_action` Canonical Matrix

**Change:** `evaluate_action` becomes canonical-native — it resolves every `(session_mode, tool_tier)` pair through an authoritative 7×3 decision matrix instead of the prior legacy-only branch ladder. Deprecated legacy modes are normalized through `_LEGACY_MODE_MIGRATION` (§22) before lookup.
**Location:** `ailienant-core/core/permissions.py` (`_DECISION_MATRIX`, `evaluate_action`, `_VALID_SESSION_MODE_VALUES`); seed propagation in `ailienant-core/core/task_service.py`.
**Schema version:** additive/behavior-preserving for legacy modes — `DEFAULT`/`AUTO`/`PLAN` produce the identical decision they always did (verified by the unchanged matrix-contract tests). The function **signature is unchanged**, so all consumers (`gateway/governance.py`, `core/tool_dispatch.py`, `brain/agentic_cell.py`, `agents/*`, `tools/*`) are untouched. `gateway/governance.py` was audited and confirmed unchanged: its `AUTO` invocation gate (→`STANDARD`) and `DEFAULT` internal-task mode (→`CAUTIOUS`) preserve their documented postures exactly.

**Resolution order:**
1. **Legacy normalization** — `mode = _LEGACY_MODE_MIGRATION.get(session_mode, session_mode)`.
2. **Identity floor** — a non-`EDIT_EXECUTE_RBW` identity (Planner / Researcher / routing-only) is `DENY` for any non-READ_ONLY tier, in every mode. READ_ONLY tier clears the floor unconditionally, so its per-mode posture (e.g. `ASK_ALL` gating reads) is honored.
3. **Matrix lookup** — `_DECISION_MATRIX[mode][tier]`.

**Authoritative matrix** (`PermissionDecision`: ALLOW / HITL / DENY — "ASK" in product language == `HITL`):

| Mode \ Tier | READ_ONLY | WRITE | EXECUTE | DANGEROUS |
|-------------|-----------|-------|---------|-----------|
| `FULL_AUTO`   | ALLOW | ALLOW | ALLOW | ALLOW |
| `STANDARD`    | ALLOW | ALLOW | ALLOW | HITL |
| `CAUTIOUS`    | ALLOW | HITL | HITL | HITL |
| `ASK_EXECUTE` | ALLOW | HITL | HITL | DENY |
| `ASK_ALL`     | HITL | HITL | HITL | HITL |
| `READ_ONLY`   | ALLOW | DENY | DENY | DENY |
| `PLAN_ONLY`   | ALLOW | DENY | DENY | DENY |

**Two decided cells (vs. the prior 3-mode engine):**
- `FULL_AUTO × DANGEROUS = ALLOW` — the sole mode that auto-runs irreversible operations unprompted. It is opt-in and identity-floored (only `EDIT_EXECUTE_RBW` reaches it). Every other mode keeps DANGEROUS at HITL or DENY; the legacy "AUTO must never auto-approve DANGEROUS" invariant survives because legacy `AUTO`→`STANDARD` (DANGEROUS=HITL).
- `CAUTIOUS × WRITE = HITL` — `CAUTIOUS` gates WRITE as well as EXECUTE/DANGEROUS, making it the faithful target of legacy `DEFAULT`.

**Seed propagation:** `core/task_service.py` validates a settings-file `permission_mode` preference against `_VALID_SESSION_MODE_VALUES` (every canonical + legacy value) rather than the former hardcoded `("DEFAULT","PLAN","AUTO")` tuple, so a canonical mode seeded via settings propagates into state instead of being silently dropped. The frontend-selector mapping (`session_mode_from_frontend`) still surfaced only the legacy 3-way choice — its canonical mapping lands in §24 below.

---

## §24 — Shadow Mapping (Canonical Frontend→Backend) + YOLO Guard Wire Contract

**Change:** Two co-shipped updates to the permission surface:

### 24a — Shadow Mapping: `_FRONTEND_MODE_TO_SESSION` points at canonical targets

`session_mode_from_frontend` now maps the 3 UI selector strings directly to canonical `SessionPermissionMode` members rather than the deprecated aliases.

**Location:** `ailienant-core/core/permissions.py` (`_FRONTEND_MODE_TO_SESSION`).

| UI string | Old target (deprecated) | New target (canonical) |
|-----------|------------------------|------------------------|
| `"automatic"` | `AUTO` | `STANDARD` |
| `"ask_before_edits"` | `DEFAULT` | `CAUTIOUS` |
| `"plan_mode"` | `PLAN` | `PLAN_ONLY` |

**Backward-compat:** deprecated legacy members remain in the enum. No checkpoint that serialized `"default"` / `"auto"` / `"plan"` is affected — those strings are deserialized by `session_mode_from_channel`, not `session_mode_from_frontend`. The shadow mapping only affects *new* submissions from the UI.

**Gateway posture update:** `gateway/governance.py` `INTERNAL_TASK_MODE` changed from `SessionPermissionMode.DEFAULT` to `SessionPermissionMode.CAUTIOUS`. Semantics are identical (HITL on WRITE + EXECUTE + DANGEROUS per the §23 matrix); `CAUTIOUS` is the canonical replacement for `DEFAULT`. The module-load invariant in `gateway/handlers.py` (`session_mode_from_frontend("ask_before_edits") is INTERNAL_TASK_MODE`) is preserved.

**`main.py` plan-mode routing:** the `plan_mode` detection that raises `planner_mode_active` now checks against `PLAN_ONLY` (was: `PLAN`).

### 24b — YOLO Guard: `risk_intercept_guard()` content-aware post-filter

**Location:** `ailienant-core/core/permissions.py` (`_RISK_PATTERNS`, `_INTERCEPT_MODES`, `risk_intercept_guard`).

**Signature:**
```python
def risk_intercept_guard(
    proposed_content: Optional[str],
    decision: PermissionDecision,
    session_mode: SessionPermissionMode,
) -> tuple[PermissionDecision, List[str]]: ...
```

**Semantics:** upgrades `ALLOW → HITL` when `proposed_content` matches one or more high-risk patterns AND `session_mode` is in `_INTERCEPT_MODES` (`FULL_AUTO`, `STANDARD`). Returns `(effective_decision, matched_labels)`. Always a no-op when `decision` is already `HITL` or `DENY`.

**Pattern registry** (`_RISK_PATTERNS` — extendable dict):

| Label | Matches |
|-------|---------|
| `privilege_escalation` | `sudo`, `su -`, `su root`, `doas` |
| `mass_deletion` | `rm -rf`, `rm -r`, `rmdir /s`, `del /s/q/f` |
| `network_egress` | `curl`, `wget`, `nc`, `netcat`, `socat` |
| `secret_access` | `.env`, `api_key`, `secret_key`, `aws_secret`, `private_key` |
| `package_install` | `npm install`, `pip install`, `apt-get install`, `brew install`, `cargo install`, `gem install` |

**Integration sites:**
- `tools/execution_tools.py` — after `gate_execute_action()`, before HITL emission.
- `tools/coder_tools.py` — after `evaluate_action()`, before HITL emission.
- `brain/agentic_cell.py` — in the main tool-call loop after `_classify_execute()` returns `"allow"`, and in `_approve_exec()` to determine `request_kind`.

**Wire contract additions to `HITLApprovalRequestPayload`** (`api/ws_contracts.py`):
```python
risk_patterns_matched: Optional[List[str]] = None
```
Populated only when `request_kind == "RISK_INTERCEPT"`. Additive field — absent on all prior HITL cards; consumers tolerate its absence.

**New `request_kind` value:** `"RISK_INTERCEPT"` — emitted when the YOLO Guard upgrades the decision. Existing `request_kind` values (`FILE_WRITE`, `BUDGET_OVERFLOW`, `SANDBOX_DEGRADED_EXEC`, `MCP_TOOL_CALL`, `COMMAND_EXEC`) are unaffected.

**Frontend:** `HITLInterventionCard.tsx` renders `RISK_INTERCEPT` cards with the `alert` icon and the matched pattern labels. The `HITLIntervention` interface gains `risk_patterns_matched?: string[] | null`.

---

## §25 — Context Pipeline `STATE_COMPACTED` Wire Contract

**Change:** additive WS server event emitted when the five-layer `ContextPipeline`
(`brain/context_pipeline.py`) FIFO-evicts Layer 4 (Conversation) turns to fit the token budget.

**New WS event** `ServerStateCompactedEvent` (`api/ws_contracts.py`), discriminator
`event_type="state_compacted"`, payload:
```python
class StateCompactedPayload(BaseModel):
    session_id: str
    compaction_message: str   # system status line — NOT AI-generated prose
    turns_compressed: int
```

**Producer:** `WebSocketConnectionManager.broadcast_state_compacted(session_id, compaction_message, turns_compressed)` (`api/websocket_manager.py`), wired to the pipeline's `on_compacted` async callback via `functools.partial` with `session_id` bound (callback arity `(compaction_message, turns_compressed)`).

**Field-name ratification (`summary` → `compaction_message`):** the originating plan named the
payload field `summary`. It ships as `compaction_message` because the value is a deterministic
system status line ("Compacted N conversation turn(s) to fit the context window."), not an
AI-generated narrative summary — the eventual prose summary is a separate concern owned by
`brain/summarizer.py`. Naming the field `compaction_message` prevents a future consumer from
rendering a status string where it expects model prose.

**Consumer:** none yet on the wire — the frontend `SessionSummaryCard` is a later milestone.
Per the additive-only contract discipline (CLAUDE.md §10), existing consumers tolerate the
unknown event; the new variant is purely additive to the `WebSocketMessage` union.

**Backward-compat:** additive event + additive union member. No existing event, field, or
checkpoint shape is altered.

---

## §26 — Devcontainer Host Execution Bridge (Trusted-Tier WS Contract)

**Change:** five additive WS events that let the backend `DevcontainerSandboxAdapter`
(`core/sandbox.py`) route trusted-tier provisioning and command execution over the existing
host socket — the host owns the local container runtime and runs `devcontainer up` / `exec`.
This is the version-tagged realization of the non-normative wire sketch in `PHASE_8.13_BLUEPRINT.md §3.2`.

**New events** (`api/ws_contracts.py`), all carrying a `request_id` (UUID4) correlation key so
concurrent commands on one session never cross-talk and a retried inbound frame is idempotent:

| Direction | `event_type` | Payload |
|---|---|---|
| server → host | `server_devcontainer_provision_request` | `session_id, request_id, cwd` |
| host → server | `client_devcontainer_provision_status` | `session_id, request_id, state` |
| server → host | `server_devcontainer_exec_request` | `session_id, request_id, command, cwd, env_keys: List[str]` |
| host → server | `client_devcontainer_exec_stream` | `session_id, request_id, stream, chunk` |
| host → server | `client_devcontainer_exec_exit` | `session_id, request_id, exit_code` |

**`state` semantics:** `state ∈ {provisioning, ready, timeout, failed}`. `provisioning` is an
interim progress tick (consumed by the runtime panel); `ready`/`timeout`/`failed` are terminal and
are the only states that resolve the provision waiter. `stream ∈ {stdout, stderr}`.

**Secret hygiene:** `env_keys` carries allowlisted environment-variable **names only — never
values** (`Field(default_factory=list)`, optional). The host resolves the values from its own
environment when invoking `devcontainer exec`, so no secret value transits the loopback bridge.
This is the wire encoding of the blueprint's "`env_whitelist` is not a wire field" — names are not
secrets; the concrete bridge maps `env_whitelist` dict → `list(env_whitelist.keys())`.

**Producer/transport:** `ConnectionManager` (`api/websocket_manager.py`) gains
`emit_devcontainer_provision_request` / `emit_devcontainer_exec_request` (server→host) and the
waiter primitives `wait_devcontainer_provision` / `wait_devcontainer_exec` (request-id keyed,
mirroring `wait_patch_ack`), resolved by `resolve_devcontainer_provision` (terminal-only),
`append_devcontainer_stream`, `resolve_devcontainer_exit`. A disconnect reaps any in-flight waiter
(`_reap_client_state`) so no path hangs.

**Consumer:** the receive-loop dispatch (`main.py`) and the concrete `HostExecutionBridge`
implementation that consumes these primitives are wired in a later sub-phase; the contract +
transport ship first. Per the additive-only discipline (CLAUDE.md §10), existing consumers tolerate
the unknown events; all five are purely additive `WebSocketMessage` union members.
