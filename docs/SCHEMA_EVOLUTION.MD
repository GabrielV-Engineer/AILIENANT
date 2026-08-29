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

    # --- Planning Mode ---
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

The broker reuses `vfs_manager.request_human_approval(...)` (same pattern as `finops_gate`). Distinguished via sentinel:
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

**Additive only. No renames, no removals, no Literal narrowing. **

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

- **New WS event** `ServerThinkingChunkEvent` (`api/ws_contracts.py`), discriminator `event_type="server_thinking_chunk"`, payload `ThinkingChunkPayload{ session_id: str, delta: str, token_count: int = 0, source: "native"|"simulated" = "native" }`.
- **`source` provenance field (additive):** `ThinkingChunkPayload.source` and the analyst-pane twin `NattThinkingChunkPayload.source` tag a reasoning trace as `native` (the model's own reasoning channel) or `simulated` (a prompt-scaffolded fallback for models without native thinking, produced by `LLMGateway.astream_reasoning`). Default `native`; omitted by older clients with no behavior change.
- **New analyst WS event** `ServerNattThinkingChunkEvent`, discriminator `event_type="server_natt_thinking_chunk"`, payload `NattThinkingChunkPayload{ session_id, delta, token_count = 0, source = "native" }` — the `natt_*`-channel twin of the main reasoning event so the analyst pane streams reasoning without colliding with the main chat.
- **`AnalystQueryPayload` growth:** `enable_native_thinking: bool = True` mirrors the master reasoning toggle into the analyst request. Omitted → reasoning on.
- **`TaskPayload` growth** (`core/task_service.py`): `enable_native_thinking: bool = True`, `thinking_budget_tokens: int = 4096`. Omitted field from a client keeps prior behavior.
- **No persisted/graph schema:** reasoning text (native or simulated) is transport-only — not a graph-state channel, not written to the checkpointer, excluded from `PERSIST_TRANSCRIPT`.

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
| `is_indexing_complete` — `Deprecated:` | `bool` |
| `guardrail_failed` | `bool` |
| `validation_feedback` | `Optional[str]` |

> **Deprecated:** `is_indexing_complete` was removed (DEBT-146, 13.x debt sweep). Declared "seeded from `lazy_indexer.is_complete` at graph invocation," but its only write site (`core/task_service.py`) always hardcoded `True`, and no node ever read it — `brain/orchestrator.py`, the presumed consumer, was itself deleted as dead code in 12.12. The real, live indexing-status signal is `LazyIndexer.is_complete`, broadcast over WS (`broadcast_indexing_complete`) straight to the frontend's `indexing` store; re-deriving a per-turn graph-state snapshot of it would duplicate that channel, and actually gating turns on it would reverse the deliberately non-blocking "lazy" indexing design — out of scope for a dead-state cleanup.

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
- **Write-through wired (12.3):** `core/tool_dispatch.py::promote_tool_state` decodes an allowlisted tool's observation (`todo_write` → `agent_todos`) back into a typed delta, which `brain/agentic_cell.py` folds into its returned delta so the channel actually receives the write through `_merge_todos`. See `SCHEMA_EVOLUTION.MD §42` for the accompanying WS broadcast.

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

`mcp_server_endpoint` is now written by the MCP auto-connect bootstrap during the FastAPI lifespan rather than only by manual calls.

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

The following event types were added to `api/ws_contracts.py`. The full union now contains 60+ events. Grouped by functional area:

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
`client_analyst_query` · `server_natt_message` · `server_natt_token` · `server_natt_thinking_chunk` · `server_natt_stream_end`

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
`server_thinking_chunk` (+ additive `source` provenance; simulated fallback via `astream_reasoning`)

**Context pipeline:**
`state_compacted` (see §25)

**Devcontainer host bridge:**
`server_devcontainer_provision_request` · `client_devcontainer_provision_status` · `server_devcontainer_exec_request` · `client_devcontainer_exec_stream` · `client_devcontainer_exec_exit` (see §26)

**Devcontainer interactive session bridge:**
`server_devcontainer_session_open` · `client_devcontainer_session_opened` · `server_devcontainer_session_stdin` · `server_devcontainer_session_signal` · `server_devcontainer_session_flow` · `server_devcontainer_session_close` · `client_devcontainer_session_stream` · `client_devcontainer_session_exit` (see §43)

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
**Location:** `ailienant-core/core/hitl.py` (`request_graph_approval`, `extract_pending_interrupt`); consumed by `brain/finops.py`, `brain/apply_gate.py`, `brain/agentic_cell.py`; resumed by `core/task_service.py::resume_graph` + the `main.py` WS handler.
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
    compaction_message: str          # system status line — NOT AI-generated prose
    turns_compressed: int
    summary_text: Optional[str] = None  # additive: AI prose for the fold, when produced
```

**Producer:** `WebSocketConnectionManager.broadcast_state_compacted(session_id, compaction_message, turns_compressed, summary_text=None)` (`api/websocket_manager.py`), wired to the pipeline's `on_compacted` async callback via `functools.partial` with `session_id` bound. The pipeline path calls with arity `(compaction_message, turns_compressed)`; the `brain/summarizer.py` path adds the optional third `summary_text` positional — both are compatible because the field defaults to `None`.

**Additive `summary_text` (prose for the fold):** the AI-generated summary produced by
`brain/summarizer.py` on a successful compression now optionally rides in this payload so the
IDE `SessionSummaryCard` can render it in the collapsed fold. It is `None` on the
truncation-fallback path (no prose produced) and from any older/pipeline sender — consumers
MUST tolerate its absence and fall back to `compaction_message`. This does not repurpose
`compaction_message`, which remains the deterministic status line.

**Field-name ratification (`summary` → `compaction_message`):** the originating plan named the
payload field `summary`. It ships as `compaction_message` because the value is a deterministic
system status line ("Compacted N conversation turn(s) to fit the context window."), not an
AI-generated narrative summary — the eventual prose summary is a separate concern owned by
`brain/summarizer.py`. Naming the field `compaction_message` prevents a future consumer from
rendering a status string where it expects model prose.

**Consumer:** the frontend `SessionSummaryCard` folds the pre-compaction transcript behind a
collapsible card, rendering `summary_text` as the fold body (falling back to a status line when
absent). Per the additive-only contract discipline (CLAUDE.md §10), older consumers tolerate the
unknown event and the unknown field; both the event and the field are purely additive.

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

**Consumer:** the receive-loop dispatch (`main.py`) and the concrete `HostExecutionBridge` implementation
(`api/devcontainer_bridge.py::WebSocketHostBridge`, injected from the composition root) that consumes these
primitives are now **wired**; the extension host handler (`providers/devcontainerExecHandler.ts`) drives the
`DevcontainerProvisioner` and emits the client→backend events. Per the additive-only discipline (CLAUDE.md
§10), existing consumers tolerate the unknown events; all five are purely additive `WebSocketMessage` union
members.

**Scope note:** this section covers one-shot command execution only (`sandbox_bash` dispatch). §43 adds a
persistent interactive session over the same host bridge, complementing rather than replacing this contract —
`HostExecutionBridge.open_host_session` (declared here, previously raising `SandboxSessionError`
unconditionally) is what §43 implements.

---

## §27 — Symbol-Definition Catalog (Tier-2 Wire Contract)

Ships the Tier-2 substrate recorded below in "Design Decision — Symbol-Level Call-Graph Substrate
(GO — Two-Tiered Hybrid)". Additive; no FK into `dependency_graph`; **no call edges are stored.**

```sql
CREATE TABLE IF NOT EXISTS symbol_definitions (
    qualified_name TEXT    NOT NULL,
    file_path      TEXT    NOT NULL,
    kind           TEXT    NOT NULL,   -- function | class | method
    start_line     INTEGER NOT NULL,  -- 1-indexed
    end_line       INTEGER NOT NULL,  -- 1-indexed
    project_id     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (qualified_name, file_path, start_line, project_id)
)
```

Population: `core.ast_engine.collect_symbol_defs` (dotted FQN, module → class → method) rides the
existing single tree-sitter parse in `brain.memory.index_file_sync`, scoped to the same languageIds
as `IMPORT_EXTRACTORS` (python, typescript(react), javascript(react)) — the import-path confidence
anchor below only exists for those languages. Persisted via `core.indexer.ReactiveIndexer.index`
under the existing content-hash short-circuit (condition #2 — no bespoke second indexer);
`upsert_symbol_definitions` replaces a file's rows per reindex.

**Query-time resolution (`core.symbol_refs.find_symbol_callers`, new module):** anchors the
symbol's defining file(s) via `get_symbol_definitions`, narrows candidates via `fts_narrow_catalog`
(the existing DEBT-041 FTS5 trigram line-index), then AST-confirms each candidate as a real
identifier reference (rejecting string/comment noise and the definition site itself), tagging
`EXTRACTED` (caller resolves to a defining file via `core.blast_radius.compute_blast_radius`) /
`AMBIGUOUS` (≥2 defining files) / `INFERRED` (confirmed reference, no import path — e.g. dynamic
dispatch; never discarded). Bounded by a wall-clock timeout and a bounded parse-fan-out semaphore,
with import-connected candidates ordered first so a timeout sheds low-value noise, never the
high-confidence hit; the result carries a truthful `truncated`/`timed_out` flag.

**Conditions #7/#8 (input sanitization, search-scope confinement) are met by construction, not by a
hardened subprocess:** `fts_narrow_catalog` was already a parameterized `MATCH ?` query scoped to
the indexed catalog (built under DEBT-041) — no ripgrep/shell subprocess was introduced. An
identifier-shaped allowlist on the symbol name remains as defense-in-depth ahead of the search.

**Consumer:** `find_symbol_callers`, a new `ToolPrivilegeTier.READ_ONLY` perception tool
(`tools/perception_tools.py::FindSymbolCallersTool`), reachable via both `build_analyst_tools` and
`build_researcher_tools` (mirrors `architecture_digest`'s dual-role wiring). An uncatalogued or
empty result is surfaced as "no callers found via this resolution path" — never "confirmed dead."

---

## §28 — Cross-Boundary Link Edges (WS / MCP Seam Wire Contract)

Models AILIENANT's inter-process contract — the string-keyed WS message union and the MCP tool
catalog that the extension (TS) and core (Python) communicate over, never through imports. Stored in
a **physically separate table** so the file-level code-dependency traversal (`dependency_graph` via
`_bfs_k_hop` / blast-radius / PPR / dead-code) can never observe these edges — non-pollution is
structural, not a filter replicated at every read site (mirrors `§27`'s "no FK into
`dependency_graph`" stance). Additive.

```sql
CREATE TABLE IF NOT EXISTS boundary_edges (
    source_file  TEXT NOT NULL,   -- declaring/referencing file (catalog path)
    channel      TEXT NOT NULL,   -- WS event_type literal or MCP tool name (node identity)
    kind         TEXT NOT NULL,   -- declares | handles | emits | references
    seam         TEXT NOT NULL,   -- ws | mcp
    project_id   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_file, channel, kind, project_id)
)
```

Population (`core.boundary_graph.refresh_boundary_graph`, new module): a cheap single-flight full
rebuild (~71 channels — no incremental per-file coupling). The channel node-set is read by lazy
runtime introspection of AILIENANT's own contract (`api.ws_contracts` `event_type` field defaults +
`gateway.catalog.CATALOG` names; imported lazily so `core/` never statically couples to
`api/`/`gateway/`). Each channel is narrowed over the indexed catalog via `fts_narrow_catalog`
(reused, subprocess-free) then confirmed by matching the channel as a **quoted string literal**
(`re.escape`d) — channels are always referenced quoted at real sites, so quote-anchoring rejects
substring false-positives (`run_task` ≠ `rerun_task`) and backtick/comment prose mentions alike. The
multi-second scan runs lock-free; only the atomic replace takes the per-project graph lock.

**Direction rule (deterministic — `(boundary side = path root)` × `(channel prefix)`):** a hit in
the declaration file (`api/ws_contracts.py` / `gateway/catalog.py`, matched by catalog path suffix) →
`declares`; WS `server_*` extension-side → `handles`; WS `client_*` core-side → `handles`,
extension-side → `emits`; MCP verb in `gateway/` → `handles`; anything else (non-`server_`/`client_`
prefix, unknown side) → `references`, never surfaced as a handler.

**Known fidelity boundary:** `declares` and `handles` are high-precision (a real
declaration/dispatch literal). `emits` is best-effort — a backend `server_*` emit is a typed model
construction (`ServerStreamEndEvent(data=…)`) with no channel literal at the send site, so those
emitters are **not** recoverable by literal analysis (tracked debt for a structural emit-site
resolver).

**Consumer:** `trace_cross_boundary`, a new `ToolPrivilegeTier.READ_ONLY` perception tool
(`tools/perception_tools.py::TraceCrossBoundaryTool`), reachable via both `build_analyst_tools` and
`build_researcher_tools` (mirrors `find_symbol_callers`'s dual-role wiring). Build-on-demand: the
graph is populated on first query. An empty `handlers` list is surfaced as "no handler found via
this resolution path" — never "unhandled."

---

## §29 — Observed Call-Edge Substrate (Tier-2 Wire Contract)

Persists the runtime-observation signal validated by the GO decision below. A Tier-2 table read by
`core.symbol_refs.find_symbol_callers` to raise caller confidence with a new top tier. Physically
separate, **no FK** into `dependency_graph` or `symbol_definitions` (same non-pollution stance as
`§27`/`§28`). Additive.

```sql
CREATE TABLE IF NOT EXISTS observed_call_edges (
    caller_symbol TEXT NOT NULL,   -- caller qualified_name (catalog FQN)
    caller_file   TEXT NOT NULL,   -- caller's native-absolute catalog file_path
    callee_symbol TEXT NOT NULL,   -- callee qualified_name (the query key)
    callee_file   TEXT NOT NULL,   -- callee's native-absolute catalog file_path
    project_id    TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (content_hash)
)
CREATE INDEX idx_observed_callee ON observed_call_edges(project_id, callee_symbol);
```

**File-column enrichment (declared beyond the manifest sketch):** the manifest listed only
`(caller_symbol, callee_symbol, project_id, content_hash)`. The `_file` columns are additive and
load-bearing — a qualified_name is not unique (the `AMBIGUOUS` case: same FQN in ≥2 files); the file is
what the runtime trace knows, the unit `find_symbol_callers` returns callers in
(`SymbolCaller.file_path`), and the key for the AMBIGUOUS-safe callee match.

**Population (`core.db.persist_observed_edges`) — append-only, no lock.** Populated **out-of-band** by
the offline trace harness (`core.call_trace_probe`, `persist=True` maps both traced endpoints to catalog
symbols via `innermost_symbol` and writes symbol pairs) — never a live sandbox-execution capture (tracked
DEBT). Unlike the rebuildable `boundary_edges` (delete+insert under `graph_write_lock`), this is an
*accumulating observation log*: `INSERT OR IGNORE` on the `content_hash` PK, **never a delete**. There is
no half-cleared window, so no lock is taken, and accumulation is the wire-level expression of "traces
raise confidence, never delete." `content_hash = sha1(json.dumps([project_id, caller_symbol, caller_file,
callee_symbol, callee_file]))` — JSON-serialized (injective, no field-boundary collision) and
`project_id`-scoped (it is the sole PK, but the catalog is one shared SQLite file, so omitting it would
`INSERT OR IGNORE`-drop the same edge observed in a second project). Idempotent ⇒ re-persist is a no-op.

**Consumer merge (`find_symbol_callers`):** after the static two-pass scan, `get_observed_callers`
supplies runtime-confirmed callers. A confirmed observation either **promotes** an already-found caller to
`OBSERVED` (the new rank-0 tier, above `EXTRACTED` — a direct observation outranks an import inference) or
**adds** a caller the static passes structurally cannot (pure dynamic dispatch, no identifier node).
Guarantees: an observation **only ever raises** confidence — it never demotes or removes a static
candidate; the match is **AMBIGUOUS-safe** (only observations whose `callee_file` is among the callee's
defining files count); staleness is bounded at the **read path** (an added observed caller is surfaced
only if its file is still in the indexed catalog — no reindexer coupling); and the "no callers found …
never confirmed dead" framing is unchanged.

---

## Design Decision — Runtime-Trace Edge Validation (GO — dogfood PoC, substrate deferred)

> Answers the 8.14.8 SPIKE: does a runtime call trace confirm/sharpen the static symbol-call
> resolution enough to justify a persisted trace substrate? No `call_edges` table exists to attach a
> `confidence_score` to (§27's Two-Tiered Hybrid GO ships **no stored call edges** — callers are
> resolved lazily by `find_symbol_callers` and tagged per-query). So this decision is offline and
> additive-free: a validation harness (`core/call_trace_probe.py`), not a schema change.

**Method.** `sys.monitoring` (PEP 669, stdlib — zero new dependency) records intra-project
`caller → callee` `CALL` edges, presence-only (`DISABLE` after first hit per call site keeps a full
test-slice trace tractable). Each traced callee's `(file, firstlineno)` is mapped to its catalog
symbol by **innermost line-range containment**, then reconciled against the existing
`find_symbol_callers` resolver (§27) into three buckets: **confirmed** (resolver already found this
caller), **dynamic discovery** (resolver structurally misses it — the value signal), **unobserved**
(a static candidate never exercised at runtime — reported verbatim, **never** `dead`; traces only
*raise* confidence, they never delete an edge). The dogfood measurement traces a bounded slice of
AILIENANT's own pytest (`test_micro_swarm_e2e.py`, `test_phase8_8_2_analyst_arsenal.py`) as a
subprocess, reconciling against a temp catalog built by indexing `core/`, `brain/`, `tools/`.

**Two callee-resolution defects found and fixed during the PoC build, both worth recording:**
1. **`__wrapped__` unwrap is load-bearing.** `sys.monitoring` observes the decorator's *wrapper*
   callable; a `functools.wraps` wrapper's `co_firstlineno` points into the decorator's own body, so
   an un-unwrapped callee silently mis-maps to the decorator, not the real target.
2. **Decorator-line drift (broader than #1 — applies to every decorated symbol, wrapped or not).**
   `co_firstlineno` for *any* decorated function points at the **first decorator line**, while
   `collect_symbol_defs` (tree-sitter) records `start_line` as the `def` line — so strict
   `[start_line, end_line]` containment silently fails to map every decorated symbol in the codebase
   (`@pytest.fixture`, LangGraph node decorators, …). Fixed with a bounded (`≤20` line) fallback: when
   no symbol contains the line outright, take the nearest symbol whose `start_line` follows within
   the bound — the decorator-prefix case — never an unrelated distant symbol.

**Measured result (dogfood, this codebase, `core/`+`brain/`+`tools/` catalog, one pytest slice):**

| Metric | Count |
|---|---|
| Observed edges (intra-project) | 305 |
| Mapped to a catalog symbol | 67 |
| Confirmed (static resolver already found it) | 40 |
| **Dynamic discoveries** (resolver structurally misses it) | **27** |
| Unobserved static candidates (never exercised by this slice — not dead) | 94 |

27 of 67 mapped edges (~40%) are dynamic discoveries — a material fraction, not noise. Concrete
example: `tool_dispatch.py`'s string-keyed dispatcher — the exact dynamic-dispatch case the Two-Tiered
Hybrid's `INFERRED` tier exists for — surfaces as an **unobserved** static candidate in this slice
(correctly not "dead": simply not exercised by these two test files), while the same `BaseTool._arun`
methods, invoked directly by `test_phase8_8_2_analyst_arsenal.py`, register as **dynamic discoveries**.
This is the literal scenario the SPIKE was designed to surface.

**Decision: GO.** The signal is real and material. A persisted `observed_call_edges` substrate +
resolver confidence-promotion (never demotion — the coverage caveat is binding) is deferred to
`8.14.8.1`, gated behind this GO, mirroring the §27 SPIKE → implementation split:
- Additive Tier-2 table `observed_call_edges (caller_symbol, callee_symbol, project_id, content_hash)`,
  idempotent ingest keyed by `content_hash` (§5.3).
- `find_symbol_callers` promotes a caller already tagged `INFERRED`/`AMBIGUOUS` to a new `OBSERVED`
  (or raises confidence within the existing tiers) when a runtime edge confirms it — never removes an
  unconfirmed candidate.
- Trace capture itself stays out-of-band (dogfood / offline harness first; a sandbox-execution-
  integrated capture is tracked as DEBT, not built here) — this decision validates the *signal*, not
  the *production capture path*.

**Known limitations (recorded so the numbers above are read correctly, not inflated):**
class-construction (`Foo()`) and `functools.partial` callees are best-effort/may be uncredited;
decorators that do not set `__wrapped__` (not `functools.wraps`-based) cannot be unwrapped, so their
callee is attributed to the wrapper's own defining symbol; `ProcessPoolExecutor` children run in a
separate interpreter and are not traced in-process — itself a finding, since that is the same
indirection static analysis also misses; C-level callees have no `__code__` and are skipped by design.
Python-only (`sys.monitoring` is CPython-specific) — TS/JS runtime tracing is out of scope for this
PoC and tracked as DEBT.

---

## Design Decision — ADR-as-Graph State (REJECTED)

> Not a wire contract (no schema was added), so this record carries no `§N` number. It is the
> durable rejection rationale for the question "should Architecture Decision Records become live,
> agent-consumed system state?"

**Decision: NO-GO.** Architecture Decision Records will **not** become live system state (no
`architecture_decisions` table, no `REFERENCES` edges read by the analyst). Architectural
decisions remain timeless Markdown in blueprints and this document, and reach agents through the
mechanisms that already exist.

**Rationale:**

- **No corpus to graph.** ADR references live only as narrative citations inside blueprints and
  `DEV_JOURNAL.md`. There is no ADR registry and no decisions table; the `manage_adr` /
  `get_architecture` capability belongs to the *external* reference graph, not this system's own
  catalog. GO would mean bootstrapping an entire corpus plus its curation surface from scratch.
- **The need is already met.** `AILIENANT.md` project-instructions inject binding
  "always do X / never do Y" standing guidance into every task
  (`core/project_instructions.py`); the analyst tri-brain already ingests a `docs` + `codex`
  brain source (§16) — it reads architectural docs into agent context already, just unstructured;
  and the active blueprint is binding by charter (§1). A decisions table + reference edges would
  be redundant with all three.
- **It would fight the timeless-documentation invariant.** Making decisions live, agent-consumed
  state means agents reason over decision identifiers and re-emit them into code comments and
  journal entries (models cite their reasoning) — re-polluting exactly what the charter's
  reference-eradication rule keeps timeless, downgrading a hard scrub rule to a soft prompt-level
  hope.
- **Stale-edge cost.** Reference edges keyed by file path rot on every rename/move (the same
  fragility the resolved-import work fights), and a stale decision edge silently mis-informs the
  agent — a wrong-direction failure.
- **Precedent.** The standing preference for an existing mechanism over a new custom subsystem
  (e.g. the hand-rolled `degree_centrality` over a heavyweight dependency) points away from
  standing up a parallel subsystem here.

**Affirmed alternative:** decisions stay in blueprints + `SCHEMA_EVOLUTION.MD`, surfaced to agents
via the analyst `docs`/`codex` brain and `AILIENANT.md` standing guidance.

**Revisit only if** the shape of the problem changes — multi-project/team scale where cross-repo
decision provenance matters, or a *measured* case of the agent re-litigating a settled tradeoff
that the existing prose channels demonstrably failed to prevent. Absent that evidence, this stays
closed.

---

## Design Decision — Symbol-Level Call-Graph Substrate (GO — Two-Tiered Hybrid)

> This record is the durable design rationale, not the wire contract itself — the shipped
> `symbol_definitions` DDL and `find_symbol_callers` tool contract now live in **`§27`** above,
> delivered under the follow-on build item `8.14.6.1`.

**Decision: GO**, but not the substrate the manifest's own text sketched (`call_edges`,
`caller_symbol → callee_symbol`, populated at index time). Research this session confirmed there is
today no symbol-level infrastructure at all — no FQN builder, no call-expression walker beyond a
narrow import/require-call match inside `_extract_ecmascript_imports`, and `_count_top_level_symbols`
(`brain/memory.py:39-42`) is a bare scalar with no identity. A stored-edges design was also found to
be structurally incompatible with the existing graph: symbol-level edges would vastly exceed
`MAX_GRAPH_EDGES=5000` (enforced as a pre-build cap-and-skip guard in `calculate_ppr_sync`/
`calculate_graph_analytics_sync`, `brain/memory.py:242-247, 339-344`), so storing them would either
blow the cap or fork the graph into two incompatible analytics systems. The adopted design instead
sidesteps edge storage entirely.

### The Two-Tiered Hybrid design

**Tier 1 (macro, unchanged).** The existing file-level `dependency_graph` + in-RAM NetworkX
Louvain/PPR/centrality pipeline is untouched — no fork, no cap redesign.

**Tier 2 (micro, new — design only; no schema/code lands under 8.14.6).** A lightweight, additive
**symbol-definition catalog**, tree-sitter indexed at file-index time, storing only symbol
*definitions* (`qualified_name, file_path, kind, start_line, end_line, project_id` — no FK into
`dependency_graph`). Row count is bounded by symbol count, not call count — a fundamentally smaller
and more predictable growth curve than an edges table, and it never enters `MAX_GRAPH_EDGES`
accounting because it is not a graph. **No call edges are ever stored.** The fragile part of any
static call graph — cross-module FQN resolution under dynamic dispatch — is sidestepped, not solved.

"Who calls this symbol" is answered lazily, at runtime, in two passes: (1) a text-search pass over the
already-indexed file set for candidate usages of the symbol name; (2) an AST-validation pass (via the
existing `core.ast_engine`) confirming each candidate is a real identifier reference, not a string
literal or comment. Import-scoped resolution (`resolve_target_to_file`/`IMPORT_EXTRACTORS`, from
8.14.0) is used **only as a ranking/confidence signal, never as a discard filter**: import-connected
candidates are tagged with a higher-confidence tier, reusing the existing `EXTRACTED`/`AMBIGUOUS`/
`INFERRED` vocabulary (`brain/memory.py::_resolve_edge_confidence`) rather than inventing new tier
names, and unconnected-but-AST-validated candidates are still returned, tagged lower-confidence. A
hard import gate was considered and rejected: this codebase's own `ToolDispatcher.dispatch()`
(`core/tool_dispatch.py:205`) resolves tools purely through a string-keyed dict lookup with zero
import relationship to the tool classes it invokes — a hard import gate would silently discard exactly
that class of dynamic-dispatch caller, reintroducing the blind spot the hybrid design exists to avoid.

Output is advisory only: gated behind the existing `ToolPrivilegeTier.READ_ONLY` tier, and — as a
structural, code-level invariant, not just a comment — never the sole trigger for a destructive action
(none exists today; this is a forward guarantee). `detect_dead_code` and blast-radius remain the
file-level signals already shipped; Tier 2 may inform a human or agent's manual judgment but never
feeds an automated deletion path, and an unconfirmed-or-empty Tier 2 result must be surfaced as "no
callers found via this resolution path" — never as "confirmed dead" — mirroring the coverage caveat
already binding on 8.14.8's runtime-trace validation ("never-observed ≠ dead").

### Why this beats the alternatives

- **vs. bespoke SQL recursive CTEs:** moot — a CTE still requires edges to exist first. The hard part
  was never the query shape, it was producing correct edges under dynamic dispatch; storing zero
  edges sidesteps that rather than querying it more cleverly.
- **vs. bundling LSP/SCIP:** a persistent language-server process or SCIP indexer buys near-perfect
  resolution at the cost of new process-lifecycle management, a large dependency footprint (against
  CLAUDE.md §9's narrow-auditable precedent — the `scipy` rejection), and its own execution-sandboxing
  surface — disproportionate for a read-only lookup, and already ruled out for this division via
  `DEBT-075` (`docs/PROJECT_MANIFEST.md:723`).
- **External validation:** an offline GraphRAG-MCP reference graph, indexed against this exact repo,
  already computes function-level `CALLS` edges and was used at 8.14-planning time to validate that
  this class of resolution is achievable here — confirming the *goal* is reachable without adopting
  that reference implementation's heavier, fully-materialized-edges approach.

### Binding conditions for the follow-on build (not built under this item)

1. Concrete additive DDL for the definition catalog; no FK into `dependency_graph`.
2. Reuse the existing incremental-reindex trigger (LazyIndexer/content-hash) for catalog freshness —
   no bespoke second indexer.
3. Import-scoped resolution is a ranking/confidence signal only, never a discard filter (see above;
   corrects an initial draft of this decision that proposed a hard filter).
4. Explicit runtime timeout + result/byte caps on the live lookup (CLAUDE.md §5.1 + §5.5); since
   condition 3 does not hard-filter, the cap must apply **post-ranking** (import-confirmed hits
   surfaced first) with a truthful `truncated: true`-style indicator, not a silent pre-ranking drop
   that could discard the real dynamic-dispatch hit before it is ever ranked.
5. The false/missing-edge rate must be empirically validated at build time against this codebase's own
   dynamic-dispatch patterns (`build_*_tools`/`register_*_tools`), not assumed.
6. `ToolPrivilegeTier.READ_ONLY` + a structural invariant that Tier-2 output is never the sole trigger
   for a destructive action; an unconfirmed/empty result is surfaced as "no callers found," never
   "confirmed dead."
7. Text-search input sanitization (CLAUDE.md §6.2): the symbol name driving the live search is
   agent-reachable and must be validated against an identifier-shaped allowlist (or passed as a
   fixed-string/literal match, never a raw regex) before reaching a subprocess call, invoked with an
   argument list — never a shell-interpolated string.
8. Search-scope confinement: the text-search pass runs only over the already-indexed file set (the
   same universe as `list_indexed_files`, which already excludes vendor/build/ignored directories),
   not a raw whole-disk invocation relying solely on the search tool's own ignore-file handling.

**Revisit trigger:** none — this is an affirmative GO. `8.14.6.1` (`docs/PROJECT_MANIFEST.md`) carries
the concrete build spec forward, binding all eight conditions above.

---

## §30 — Dynamic Subagent Dispatch Schema + State Channels

New module `ailienant-core/brain/subagent_contracts.py` and four additive `AIlienantGraphState`
channels. The schema foundation for Division 8.15 (Dynamic Subagent Dispatch): every LLM-authored
dispatch instruction is a Pydantic-validated closed-vocabulary object, parsed exactly as a
`MissionSpecification` is — never executed as code. Additive only.

> **Section number.** The blueprint reserved this record as "§27," written when the then-current max
> was §26; Division 8.14 subsequently shipped §27–§29, so the dispatch schema lands as **§30**. No
> content collision — purely a numbering reconciliation.

**Contracts (`brain/subagent_contracts.py`).** Six models, all closed-vocabulary (no free-form
JSON-schema dict):

- `SubagentResponseField` — `{name: str, type: Literal["str","int","float","bool","list_str"], description: str}`.
- `SubagentResponseSchema` — `fields: List[SubagentResponseField]`, bounded `[1, 8]`.
- `SubagentTask` — `task_id`, `description` (≤4000), `subagent_role`
  (`Literal` of the 8 canonical roles + `analyst_readonly`), `response_schema`,
  `context_refs: List[str]` (≤20, VFS paths only), `max_iterations` (`[1, 8]`, default 1).
  **Security invariant:** carries no `session_permission_mode` field — there is no wire path for a
  subagent to request a more permissive mode than its parent turn.
- `DispatchPlan` — `pattern` (`Literal` of the 6 dispatch patterns), `tasks: List[SubagentTask]`
  bounded `[1, 32]` (fan-out width ceiling), `synthesis_instruction` (≤2000), `dispatch_depth`
  bounded `[0, 2]` (recursion ceiling). The bounds are the first line of defence against an
  over-scoped plan (rejected at construction, not after spawning work).
- `SubagentResultEnvelope` — `task_id`, `status` (`Literal["ok","error","budget_exhausted","denied"]`),
  `structured_result: Optional[Dict[str, Any]]`, `raw_digest` (≤`MAX_OBSERVATION_CHARS`, always
  pre-truncated), `cost_usd`, `iterations_used`, `error_message`.
- `DispatchBatchResult` — `batch_id`, `pattern: str` (open on the result side), `results`,
  `total_cost_usd`, `winner_task_id: Optional[str]`.

**Single-sourced cap.** `SubagentResultEnvelope.raw_digest`'s ceiling is
`shared.config.MAX_OBSERVATION_CHARS` (= 4000), the same constant `core/tool_dispatch.py`'s dispatch
loop enforces (`_MAX_OBSERVATION_CHARS` now aliases it). The two truncation ceilings can never drift.

**State channels (`brain/state.py::AIlienantGraphState`).** Four additive channels, all default-safe
so a pre checkpoint deserializes unchanged — no existing field is renamed or narrowed, and
consumers written before ignore the new fields entirely:

| Channel | Type | Reducer | Safe default |
|---|---|---|---|
| `dispatch_plan` | `Optional[Dict[str, Any]]` (`DispatchPlan.model_dump()`) | scalar overwrite | `None` |
| `dispatch_batch_result` | `Optional[Dict[str, Any]]` (`DispatchBatchResult.model_dump()`) | scalar overwrite | `None` |
| `dispatch_depth` | `int` | scalar overwrite | `0` |
| `subagent_dispatch_trace` | `Annotated[List[Dict[str, Any]], operator.add]` | append (`operator.add`) | `[]` |

The plan/result channels store `model_dump()` dicts (not the Pydantic models) so the checkpoint stays
plain-JSON-serializable and `state.py` imports nothing from `subagent_contracts`. The `_dispatch_results`
fan-in channel is **not** part of this record — it lands with the dispatch worker in a later sub-phase.

---

## §31 — Dynamic Dispatch Mechanics Channels

Two additive `AIlienantGraphState` channels + the dispatch primitive/worker/synthesis nodes that build on
the §30 schema. Additive only — a pre-8.15 checkpoint deserializes both to their safe defaults; no existing
field is renamed or narrowed.

| Channel | Type | Reducer | Safe default |
|---|---|---|---|
| `_dispatch_results` | `Annotated[List[Dict[str, Any]], operator.add]` (per-worker `SubagentResultEnvelope.model_dump()`) | append (`operator.add`) | `[]` |
| `dispatch_wave_count` | `int` (waves already dispatched at this level; single-writer) | scalar overwrite | `0` |

**New code (`brain/dispatch.py`, `brain/nodes/subagent_worker_node.py`, `brain/nodes/dispatch_synthesize_node.py`):**
`build_dispatch_sends(plan, base_state, *, target_node="subagent_worker")` emits one `Send` per
`SubagentTask` in the current wave (slice `tasks[wave*cap : (wave+1)*cap]`, `cap =
shared.config.MAX_CONCURRENT_SUBAGENTS`, default 4), incrementing `dispatch_depth` at the fan-out edge.
`subagent_worker` runs a role-gated `ToolDispatcher` loop, synthesises a `response_schema`-constrained
answer, and writes one envelope to `_dispatch_results`. `dispatch_gate` (single fan-in node) advances
`dispatch_wave_count`; `route_after_workers` loops back for the next wave or proceeds to
`dispatch_synthesize`, which digests the accumulated envelopes under a per-batch char ceiling keyed off the
parent's `active_llm_profile` and writes one `DispatchBatchResult` to `dispatch_batch_result`.

**Amendment to the §30/blueprint contract:** `_dispatch_results` is **never cleared mid-dispatch**. An
`operator.add` channel cannot be reset (returning `[]` concatenates to a no-op), so — rather than a custom
reset-sentinel reducer — `dispatch_synthesize` is **terminal** (runs once after the final wave, digests all
accumulated results), the wave loop-back lives in `route_after_workers` on the `dispatch_gate` fan-in node,
and the raw channel is simply superseded on the next dispatch invocation. This preserves R6 (`operator.add`
from day one) and bounds peak concurrency at the cap.

**Deferred (not this record):** production graph wiring + the `AILIENANT_ENABLE_DYNAMIC_DISPATCH` feature
flag + planner/researcher entry edges (8.15.5); budget-admission ledger (8.15.4); `analyst_readonly`
READ_ONLY floor-lock and per-role dev tool arsenals (8.15.5); tournament winner selection (8.15.3).

---

## §32 — Dispatch Production-Wiring Channels

Six additive `AIlienantGraphState` channels supporting the graph-wired dispatch subgraph (the §30 schema +
§31 mechanics folded into `brain/engine.py` behind `AILIENANT_ENABLE_DYNAMIC_DISPATCH`). Additive only — a
pre-8.15 checkpoint deserializes each to its safe default; no existing field is renamed or narrowed.

| Channel | Type | Reducer | Safe default | Role |
|---|---|---|---|---|
| `dispatch_return_node` | `Optional[str]` | scalar overwrite | `None` (→ `"drift_compute"`) | downstream node the subgraph rejoins after synthesis |
| `dispatch_round_count` | `int` | scalar overwrite | `0` | pattern loop-back counter (adversarial critic / loop continuation), distinct from `dispatch_wave_count` |
| `_dispatch_consumed` | `int` | scalar overwrite | `0` | result-isolation watermark: synthesis digests/commits only `_dispatch_results[consumed:]` |
| `_dispatch_reserved_usd` | `float` | scalar overwrite | `0.0` | running budget reservation for the active dispatch (folded into `current_cost_usd` via its `operator.add` reducer) |
| `_dispatch_reserved_round` | `int` | scalar overwrite | `-1` (sentinel) | highest round already reserved — reserve is single-flight/replay-safe per round |
| `_dispatch_admission` | `Optional[str]` | scalar overwrite | `None` | origin verdict (`"admit"|"denied"|"budget_exhausted"`) read by the admission router |

**New nodes (`brain/dispatch.py`):** `dispatch_origin` is now the **admission gate** (validate → depth/width
deny with bounded rejection envelopes ≤ `MAX_DISPATCH_WIDTH` → single-flight budget reserve); a pure string
router `route_after_admission` routes to `dispatch_fanout` (Send-only fan-out) or short-circuits to
`dispatch_synthesize`. `dispatch_advance` performs a pattern round transition (swap `dispatch_plan`, reset
`dispatch_wave_count`, bump `dispatch_round_count`); `route_after_workers` is now 3-way
(next wave | new round | synthesize) and `route_after_synthesis` returns `dispatch_return_node`.

**Amendment to the §31 contract:** `dispatch_synthesize` remains **terminal per top-level dispatch**, but
because two dispatches can run in one graph turn (researcher then planner) and patterns can loop, it now
digests/commits only the watermarked slice `_dispatch_results[_dispatch_consumed:]`, advances the watermark
to `len(_dispatch_results)`, commits the round's budget reservation, and clears `dispatch_plan=None` so a
stale plan cannot re-fire an origin edge on the way back to the spine. New constants (`shared/config.py`):
`ENABLE_DYNAMIC_DISPATCH`, `MAX_DISPATCH_DEPTH` (2), `MAX_DISPATCH_WIDTH` (32), `MAX_DISPATCH_ROUNDS` (3),
`MAX_TOTAL_DISPATCH_FANOUT` (64).

---

## §33 — Session-Start Timestamp Channel (Context-Utilization Telemetry)

One additive `AIlienantGraphState` channel supporting the 8.16.0 context-utilization telemetry spike.
Additive only — a pre-8.16 checkpoint deserializes it to its safe default; no existing field is renamed
or narrowed.

| Channel | Type | Reducer | Safe default | Role |
|---|---|---|---|---|
| `session_start_time` | `Optional[float]` | scalar overwrite (`LastValue`) | `None` | wall-clock epoch (`time.time()`) marking session start, used to derive the `duration_s` field of `core/telemetry_log.py::log_context_utilization` records |

**Write discipline:** set exactly once per session by `core/task_service.py::TaskService._resolve_session_start_time`
(a `@staticmethod`), called from `_run_coding_task`'s `if resume_value is None:` branch on every ordinary
turn — not only the first. The resolver probes the warm L1 checkpoint (`hybrid_checkpointer.get_tuple`,
zero IOPS — `MemorySaver`, not SQLite) for a value already seeded on an earlier turn of the same
`thread_id` and carries it forward unchanged; only a genuinely cold thread gets a fresh `time.time()`. No
graph node ever returns this key in a state update — it is read-only downstream, exactly mirroring the
existing `session_permission_mode` channel's seed-only write discipline (never written by a node; read
back from a prior checkpoint via `aget_state` in the native-HITL-resume branch), which is the empirical
precedent this design leans on.

**Known limitation:** if the server process restarts mid-session, L1 goes cold and the resolver has no L2
fallback (avoiding a per-turn SQLite read is the point) — the next turn is treated as a fresh session
start, so `duration_s` under-counts for that one session. A degraded metric, not a correctness issue.

## §34 — Memory Dashboard: Embedding Browser + Purge (REST Wire Contract)

Additive REST surface on the existing `/api/v1/memory` router (`api/memory_dashboard.py`) for the GraphRAG
visualization's file-embedding browser. Transport-only Pydantic models (not persisted schema); additive and
version-tolerant — consumers ignore unknown fields, and no existing endpoint's shape changes.

| Endpoint | Method | Request | Response | Notes |
|---|---|---|---|---|
| `/api/v1/memory/embeddings` | GET | `project_id`, `folder`, `sort∈{file_path,token_count,indexed_at}`, `order∈{asc,desc}`, `offset≥0`, `limit∈[1,200]` | `EmbeddingListResponse{project_id, folder, rows:[EmbeddingRow{file_path,label,token_count,indexed_at,snippet}], total, offset, limit}` | Reads a bounded metadata window via `SemanticMemoryManager.list_embeddings` (no vector column), sorts by the whitelisted key + slices in Python; `total` = matched rows in the scanned window. Unknown `sort`/`order` → 422; bad `project_id` (fails `_SAFE_ID_RE`) → 400. |
| `/api/v1/memory/embeddings/purge` | POST | `{project_id, file_path, confirm:true}` | `PurgeResponse{ok, file_path}` | HITL-gated (`confirm` must be `true`, else 422). Delegates to the existing `SemanticMemoryManager.semantic_delete` (id allowlisted, path single-quote-escaped). Idempotent — purging an absent row is a no-op. |

**Cap-raise (compatible widening):** `/api/v1/memory/graph`'s `max_nodes` upper bound widened `2000 → 5000`
(default unchanged at 300), aligned with `MAX_GRAPH_EDGES`. Strictly permissive — no previously-valid request
becomes invalid; the `capped` flag contract is unchanged.

**Store method:** `SemanticMemoryManager.list_embeddings` (`core/memory/semantic_memory.py`) mirrors
`dump_vectors`' injection-proof PyArrow-Expression pushdown (`workspace_hash` bound, never interpolated) but
selects metadata columns including `indexed_at` and excludes the heavy `vector` column.

---

## §35 — Dashboard Project-Context Scoping (Registry + Additive `project_id` Columns)

Gives the dashboard a persistent notion of "active project" and lets the panels backed by genuinely
per-project data re-scope on a project switch. **Additive only** — every column is nullable, every filter is
optional, and an omitted `project_id` reproduces the prior process-global behavior, so old rows and old
callers keep working unchanged.

### 35.1 — Project Registry Table + REST Contract

The on-disk analytics stores are keyed only by the opaque `project_id` hash and never record a workspace name
or path. A new persistent mapping supplies the human-readable selector.

| Surface | Shape | Notes |
|---|---|---|
| `projects` table (`core/db.py`) | `project_id TEXT PK, workspace_root TEXT NOT NULL, last_seen_utc INTEGER NOT NULL` | Upserted on `client_workspace_init` (`main.py`). Survives restarts. Helpers: `upsert_project`, `get_all_projects`. |
| `GET /api/v1/projects` (`api/projects.py`) | `[{ id, name, path, last_seen }]` | `name = basename(workspace_root)`; `path` returned for disambiguation. **Ghost filter:** a `workspace_root` absent on disk is omitted at read time (registry row is retained — an unmounted drive may return). |

### 35.2 — Additive `project_id` Columns on the Per-Project Ledgers

Each is a nullable `TEXT project_id` added via a PRAGMA-guarded `ALTER TABLE ADD COLUMN` in that module's own
init (SQLite has no `ADD COLUMN IF NOT EXISTS`; `CREATE TABLE IF NOT EXISTS` leaves an existing table
untouched), plus a b-tree index so the `WHERE project_id = ?` read is O(log N), not a full scan. Populated at
the write site from `state["project_id"]`.

| Table | Module | Index | Write-site source |
|---|---|---|---|
| `routing_decisions` | `core/telemetry.py` | `idx_routing_decisions_project` | `log_routing_decision(..., project_id=state["project_id"])` at each `brain/engine.py` routing edge + `agents/researcher.py` |
| `oom_fallback_events` | `core/telemetry.py` | `idx_oom_fallback_events_project` | `log_oom_event` reads `state["project_id"]` |
| `hitl_audit_log` | `core/audit.py` | `idx_audit_project` | resolved from the **persisted checkpoint** via `brain.checkpoint.project_id_for_thread(session_id)` — reconnection-safe, since the transport layer holds only `session_id` (== `thread_id`). **Not** part of the blake2b chain hash (covers `session_id`), so chain verification is unaffected. |
| `dead_letter_tasks` | `core/dead_letter.py` | `idx_dlq_project` | `save_dead_letter` reads `state["project_id"]` |

### 35.3 — Optional `project_id` Read Filters (REST)

Additive query params; omitting them returns the prior global result. `GET /api/v1/telemetry/routing`,
`/api/v1/telemetry/oom`, `/api/v1/audit/log`, `/api/v1/audit/stats`, `/api/v1/dlq/pending` each accept an
optional `project_id`. `DeadLetterRecord` and the routing/OOM row dicts gain an additive `project_id` field.

**Deliberately global (not scoped):** hardware/VRAM and runtime/Docker (machine-global), the in-memory token
ledger (`token_ledger.snapshot()`), and MCP-server config. These panels surface a "machine/process-global"
badge rather than a no-op filter. Per-project token-cost bucketing is deferred (see `TECH_DEBT_BACKLOG.md`).

## §36 — Monitoring Backend Telemetry (Request Latency + Container Lifecycle)

Persists the two monitoring signals the frontend-only 11.3 could not fabricate. **Additive only** — two new
tables and two new read endpoints; nothing existing changes, and clients that ignore them are unaffected.
Both tables are append-only with **windowed/clamped reads** (never a full scan); their unbounded growth is
tracked for a future janitor slice (`# TODO(retention): DEBT-120`).

### 36.1 — `request_latency` (project-scoped)

| Surface | Shape | Notes |
|---|---|---|
| `request_latency` table (`core/telemetry.py`) | `id PK, timestamp, session_id TEXT, project_id TEXT, duration_ms REAL, outcome TEXT` | In `_PROJECT_MIGRATIONS` ⇒ nullable `project_id` + `idx_request_latency_project`. Written by `log_request_latency(...)` from `_run_coding_task`'s outermost `finally` (monotonic `perf_counter` bracket; `outcome ∈ {completed, interrupted, failed, aborted}`). |
| `GET /api/v1/telemetry/latency` | `{ count, p50_ms, p95_ms, p99_ms, max_ms, avg_ms, recent: number[] }` | Optional `project_id`. Percentiles are hand-rolled (§9, no numpy) over an SQL-`LIMIT`-bounded most-recent window (≤500); `recent` is the newest ≤60 durations (chronological) for a sparkline. Empty/uninitialised ⇒ all-zeros, never an error. |

### 36.2 — `container_lifecycle` (machine-global)

| Surface | Shape | Notes |
|---|---|---|
| `container_lifecycle` table (`core/telemetry.py`) | `id PK, timestamp, event TEXT, container_id TEXT, image TEXT, tier TEXT` | **No `project_id`** (machine-global, like the Runtime panel). Written by `log_container_event(...)`, emitted best-effort from `core/sandbox.py`'s **async** lifecycle wrappers (`_ensure_container_running`/`shutdown`, event-loop thread) so it can never affect the cage. |
| `GET /api/v1/runtime/lifecycle?limit=N` | `{ events: [{ id, timestamp, event, container_id, image, tier }] }` | Clamped, newest-first. Consumed by the RuntimePanel timeline, which pairs `started`→`stopped` by `container_id` and renders an open span (to now) when a stop never arrives. |

### 36.3 — Deliberately not built

Adapter **set-tier** control (a manual tier override) was rejected: forcing the execution tier can silently
downgrade isolation (NATIVE_HITL host-exec < Docker cage). The tier ladder stays read-only.

### 36.4 — Per-exec command log (in-memory ring, NOT a table)

Reframed from the vacuous "container-log stream" (the dashboard is HTTP-only and the cage runs `tail -f /dev/null`).
Unlike 36.1/36.2 this is **not persisted** — a bounded in-memory ring in `core/exec_log.py` (`deque(maxlen=200)`,
lock-serialized), so a restart clears it and there is no retention surface (no DEBT-120 impact). Populated by a shared
`record_execution(adapter, …, source=…)` wrapper the 6 project-work `execute()` callers route through (`run_command`,
the graph-tracked tool-stream, pre/post-patch `hook`s, `type_check`, `coder_verify`, `coder_exec`); the benchmark eval
harness and the interactive-cell PTY stream are deliberately excluded. Each entry:
`{seq, ts, session_id, source, command, exit_code, output, duration_ms}`. Both `command` and `output` are
middle-truncated and passed through `core.redaction.mask_secrets` (extracted from `telemetry.py`; the masker is now
shared) **before** the lock — the critical section is only the `seq` bump and `deque.append`.

Read contract — `GET /api/v1/runtime/exec-log?tail=<N>&since=<seq>` (machine-global):

| Field | Meaning |
|---|---|
| `entries` | Chronological (oldest→newest) list of entries with `seq > since`; without `since`, the most-recent `tail` (clamped 1..200). |
| `latest_seq` | The ring's current max `seq`; the client sends it back as `since` so an idle poll returns `entries: []`. |

---

## §37 — Coder Companion Explanation Wire Contract (Phase 11.5.B)

**Change:** new WebSocket event `server_coder_companion` carries structured explanation (objective / decisions / patterns / bottlenecks / security notes / errors / follow-ups) emitted asynchronously after CoderAgent completes a patch. Fire-and-forget, never blocks graph; rendered beside diff-approval UI.
**Location:** `ailienant-core/api/ws_contracts.py` (new `CoderCompanionPayload`, `ServerCoderCompanionEvent`); `ailienant-core/brain/coder_companion.py` (backend pipeline); `ailienant-extension/src/workspace/components/CoderCompanionCard.tsx` (frontend render).
**Schema version:** additive, backward-compatible. Consumers that ignore this event are unaffected.

**Payload:**
```python
class CompanionDecisionWire(BaseModel):
    name: str
    rationale: str
    risk: Optional[str] = None
    tradeoff: Optional[str] = None

class CoderCompanionPayload(BaseModel):
    session_id: str
    task_id: str
    correlation_id: str  # f"{task_id}:{attempt_ordinal}" — retry-safe keying, last-write-wins
    objective: str       # Full sentence summarizing what the patch achieves
    decisions: List[CompanionDecisionWire] = []
    patterns_applied: List[str] = []
    bottlenecks: List[str] = []
    security_notes: List[str] = []  # Distinct lane; populated only when security_flags non-empty
    errors_found: List[str] = []
    follow_ups: List[str] = []
    reasoning_summary: Optional[str] = None  # Reserved; always null in this phase
    degraded: bool = False  # True on any failure (timeout/parse/LLM exception)
```

**Producer contract:**
- Fired by `schedule_coder_companion(state, attempt_ordinal)` from `run_coder_node`, immediately after SEARCH/REPLACE parsing. Synchronous entry point; delegates to `asyncio.create_task(_run_coder_companion)` (fire-and-forget, never blocks `route_after_coder`).
- Emitted via `broadcast_coder_companion(session_id, task_id, correlation_id, analysis)` in `api/websocket_manager.py::ConnectionManager`.
- Best-effort: budget pre-check skip, VRAM lock busy-skip, LLM timeout/exception, parse failure all result in `degraded=True` and "Explanation unavailable" objective. No event sent if budget ceiling reached or VRAM unavailable (frontend timeout path renders identical "unavailable" state).
- Verbosity heuristic: `errors` or `security_flags` present → `deep` (all fields populated); >3 files → `deep`; 1 file no errors → `minimal`; else `normal`.

**Consumer contract:**
- Frontend `CoderCompanionCard` component subscribes to `server_coder_companion`, filters by `task_id`.
- Skeleton-first rendering: fixed-height placeholder from mount (no layout shift).
- Keyed by `correlation_id` (derived from `attempt_ordinal`): new attempt resets card to skeleton immediately.
- 8s client timeout → "unavailable" state (covers skip/timeout/loss uniformly; dev never sees the distinction).
- Renders `objective` + `security_notes` (if present, distinct lane) always-visible; `decisions` inline; `patterns_applied` / `bottlenecks` / `errors_found` / `follow_ups` behind collapse toggle (pure display-density, no re-fetch).

**Backward-compat guarantee:**
- Old clients ignoring this event type see no behavioral change (socket is multiplexed; unknown events are silently dropped by the Pydantic discriminator).
- The frontend can be deployed independently; the backend fires the event regardless.

**Known limitations (declared, not deferred):**
1. `reasoning_summary` always `null` — no live thinking-tap in this phase.
2. Retry attempt costs one small-tier LLM call (acceptable given `MINI_JUDGE_MODEL` pricing).
3. No per-turn reserve/commit budget gate (soft skip-if-over-ceiling only).
4. `imports_graph` context enrichment cut from v1.
5. Under sustained local-tier VRAM contention, Companion may degrade far more often than succeed (explicitly lower priority than user's real work).
6. Independent timeout constants (12s backend, 8s frontend); if backend latency runs close to 8s in practice, revisit together.

Cursor keys off the monotonic `seq`, never the display `ts` (tie-safe). Additive-only; old clients ignore the endpoint.

---

## §38 — Agent Activity Timeline Wire Contract (Phase 11.5.C)

**Change:** new WebSocket event `server_activity_event` — an ordered, **un-throttled** stream of structured agent-activity events (reads, edits, commands, plan/reasoning/diff markers) that powers the Glass-Box Timeline. Unlike `server_pipeline_step` (metered by the NarrationGate to ≤15% of answer bytes), this channel is exempt from throttling: it is low-volume structured metadata, not free narration, so the full activity trace survives on a coding turn. Ordering is carried by a per-turn monotonic `seq`; `kind` is a stable enum from which the frontend composes the human label (no raw internal node token — e.g. `context_gather` — ever reaches the screen).
**Location:** `ailienant-core/api/ws_contracts.py` (new `ActivityEventPayload`, `ServerActivityEvent`, `ActivityKind` Literal); `api/websocket_manager.py` (`broadcast_activity_event`, un-gated); `core/task_service.py` (turn-local `seq` + `_push_activity` + `_classify_activity`, both fed from the single `_narrate` choke point).
**Schema version:** additive, backward-compatible. Consumers that ignore this event are unaffected.

**Payload:**
```python
ActivityKind = Literal[
    "understanding", "planning", "reviewing", "read", "edit",
    "command", "retrieval", "heal", "reasoning", "plan", "diff", "cell",
]

class ActivityEventPayload(BaseModel):
    session_id: str
    seq: int                      # per-turn monotonic order key (the deterministic sort)
    ts: float                     # unix timestamp — relative-time display / tiebreak only
    kind: ActivityKind            # frontend composes the human label from this
    target: Optional[str] = None  # e.g. file path / command / tool name
    metric: Optional[str] = None  # e.g. "+18 −4", "replanning 1/3", "iteration 3"
    ref: Optional[str] = None     # correlates a marker to its body (thinking id / patch_id / tool_call_id / cell:{iteration})
```

**Producer contract:**
- One choke point: `core/task_service.py::_narrate(raw)` classifies the raw node label via `_classify_activity(raw) → (kind, target, metric)` and emits the un-throttled `server_activity_event` (timeline). **Update (11.5.C.3):** the legacy throttled `server_pipeline_step` channel and its sole consumer `PipelineProgress` are retired — `_narrate` emits `server_activity_event` only. Nodes are unchanged — they still emit raw labels.
- `seq` = a turn-local `itertools.count()`; strictly increasing per turn. `ts = time.time()`.
- **Bounded:** an un-gated channel is capped at `ACTIVITY_CAP = 500` events per turn — past it a single `command` sentinel (`metric="activity trace capped at 500"`) is emitted and the rest are dropped, so a pathological loop can never flood the webview.
- **`plan`** fires once, as soon as the WBS exists (the existing early-seed `broadcast_plan_document` latch in the graph-stream loop), `metric=f"{N} steps"`.
- **`diff`** fires once per file that actually lands on disk (never a merely-proposed file), after `apply_patch_set` confirms `ok`. `target=ref=file_path` (stable, unique within a turn — no new id needed); `metric` is a compact `"+N -M"` line-delta computed by `_diff_line_delta` over the unified diff already held in `patches`.
- **`reasoning`** fires once per `_ThinkingStreamer` span, on that streamer's first `flush()`, via an injected `on_span_start(ref)` hook. The streamer mints one `ref = uuid4().hex` per instance and stamps it on every `ThinkingChunkPayload`/`NattThinkingChunkPayload` delta of that span (`ref: Optional[str] = None`, additive on both payloads; `broadcast_thinking_chunk`/`broadcast_natt_thinking_chunk` gained a matching `ref` kwarg). **Scoped simplification:** one ref per streamer instance (per turn), not per distinct reasoning burst — if two separate reasoning spans occur in one turn (e.g. the planner's live pass and a later native coder pass), both group under one timeline node this slice; refining to one marker per burst is deferred, not silently dropped.
- **`cell`** (11.5.C.4) fires once per agentic-cell loop iteration, from `LiveCellDispatcher.emit_tool_call_start` — the natural "iteration begins" signal, mirroring the reasoning span-start precedent rather than firing on every sub-event. `target=tool_name`, `ref=f"cell:{iteration}"` (0-based, matching the existing `server_cell_*` iteration numbering), `metric=f"iteration {iteration + 1}"`. `emit_pty_chunk`/`emit_ast_diff`/`emit_governor_tick` are unaffected — their body detail continues to ride their own existing `server_cell_*` channels, correlated into the same timeline node by `ref` on the frontend. `push_activity` is threaded into `LiveCellDispatcher`'s constructor from the same turn-local `_push_activity` closure (optional, guarded — a dispatcher constructed without it, as pre-existing direct-construction tests do, stays a valid marker-free dispatcher).

**Consumer contract (frontend, 11.5.C.1–.4):**
- `useWSMessageHandler` builds a per-turn `seq`-ordered `timeline` node list; the `AgentTimeline` component (11.5.C.2) renders it, composing the human label from `kind` and correlating heavy bodies (reasoning tokens, diffs, tool output, cell iterations) into nodes by `ref`. Node-building is order-agnostic (a body event may precede its marker) — a `ref`-keyed map creates a placeholder on first sight of either.
- **`cell`** rows (11.5.C.4) reuse `CellAuditWidget` fed a synthetic single-iteration run (`{ iterations: [entry.cell] }}`), since it already owns its own per-iteration expand/collapse and live-follow logic. Rich Tool Chips (`ToolChip`/`ActionLog`) are deliberately **not** migrated: their only live data source (`execute_tracked_tool`, the dev-palette `/dev/run-bash` smoke command) is not part of the agent's turn, so there is no turn-scoped marker for them to correlate against — see `docs/TECH_DEBT_BACKLOG.md` DEBT-122.

**Backward-compat guarantee:**
- Old clients ignoring `server_activity_event` see no change (multiplexed socket; unknown events silently dropped). The backend fires it regardless of the frontend consumer's presence.

Cursor/timeline keys off the monotonic `seq`, never the display `ts` (tie-safe). Additive-only.

---

## §39 — Auto-Accept Shift-Left: Additive `TaskPayload` Field + Apply-Edge Gate

**Additive only. No `AIlienantGraphState` channel changes, no new WS event.**

- **`TaskPayload` growth** (`core/task_service.py`): `auto_accept_low_risk: bool = False`. Rides
  the existing HTTP `POST /task/submit` payload client→host→backend along the same path as
  `enable_native_thinking` (`Workspace.tsx` SUBMIT_TASK → `workspace_panel.ts` → `session.ts`).
  Omitted → `False` → an approval card is always shown, exactly as before.
- **Apply-edge gate** (`_run_coding_task`, `PermissionDecision.HITL` branch): the backend now
  decides auto-accept at the source. Each pending edit is judged on its **added diff lines** via
  the new shared `permissions.py::scan_risk_patterns(content) -> List[str]` (the label-only core
  extracted from `risk_intercept_guard`, matching the `_RISK_PATTERNS` set: privilege escalation,
  mass deletion, network egress, secret access, package install). When `auto_accept_low_risk` is
  set **and** no added line trips a pattern, the edit applies server-side and **no
  `server_hitl_approval_request` is emitted**. Any match — or the pre-existing blast-radius gate
  (`task_service.py`, still fires on both paths) — forces the manual card. The
  `ServerHITLApprovalRequestEvent` / `client_hitl_response` wire contracts are unchanged.
- **Frontend consumer change:** the prior webview auto-accept short-circuit in
  `useWSMessageHandler.ts` (which read a never-populated `risk_metrics` field — vacuously true,
  so it auto-accepted every card) was removed; the decision is authoritative on the backend.
- **Deferred (DEBT-125):** a real diff-aware/semantic edit-risk classifier and the
  `risk_metrics` (FE) ↔ `risk_patterns_matched` (BE) name-mismatch fix so the card can display why
  an edit was flagged. The current gate is a conservative proxy that fails toward the card.

---

## §40 — Execution Provenance: Glass-Box Timeline I/O Detail

**Change:** new WebSocket event `server_activity_detail` — the I/O body for one `command` node on
the §38 Glass-Box Timeline: which isolation envelope actually ran a command, its output, exit
code, and duration. Correlated to its `ActivityEventPayload` marker (`kind="command"`) by a shared
`ref` (a fresh execution id), the same body-on-a-separate-channel pattern §38 already established
for `reasoning` (`server_thinking_chunk`) and `diff` (`RENDER_DIFF`) — kept off the marker channel
itself so the cheap, un-throttled, `ACTIVITY_CAP`-bounded spine never carries a multi-KB body.
**Location:** `ailienant-core/api/ws_contracts.py` (new `ActivityDetailPayload`,
`ServerActivityDetailEvent`, `ExecutionSource` Literal); `api/websocket_manager.py`
(`broadcast_activity_detail`, un-gated); `core/activity_context.py` (new — the turn-scoped
`ActivitySink` Protocol and its binding `ContextVar`); `core/exec_log.py` (`record_execution` is
the sole emitter — see Producer contract).
**Schema version:** additive, backward-compatible. Consumers that ignore this event are unaffected.

**Payload:**
```python
ExecutionSource = Literal["devcontainer", "docker", "wasm", "native_host", "unknown"]

class ActivityDetailPayload(BaseModel):
    session_id: str
    ref: str                            # matches the ActivityEventPayload.ref of its marker
    source: ExecutionSource             # read off the adapter's own `execution_source` attribute
    cwd: Optional[str] = None
    initiator: Optional[str] = None     # record_exec's `source` arg, e.g. "coder_verify"
    stdout: Optional[str] = None        # masked + capped by core/exec_log.py::record_exec
    stderr: Optional[str] = None        # masked + capped by core/exec_log.py::record_exec
    exit_code: Optional[int] = None     # None only on the fault path (see below)
    duration_ms: Optional[float] = None
    truncated: bool = False
    error: Optional[str] = None         # set only when adapter.execute() raised
```

**Producer contract:**
- One choke point: `core/exec_log.py::record_execution` — every EXECUTE-tier call site
  (`sandbox_bash`, `check_type_integrity`, the coder's `_gated_exec`/`coder_verify` paths) already
  routed through it before this division; nothing new calls it.
- **Correlation without touching a tool signature:** `core/task_service.py::_run_coding_task` binds
  a turn-scoped `ActivitySink` via `core.activity_context.bind_activity_sink` before entering the
  graph and resets the token in the same outer `finally` that already resets the MCP session
  `ContextVar`s (charter §5.1). `record_execution` reads it back with
  `current_activity_sink()` — a `contextvars.ContextVar`, so it propagates through the LangGraph
  node → LangChain tool call stack, and through the `asyncio.to_thread` hops the Docker/Wasm tiers
  use, without any LLM-facing tool gaining a new parameter. Outside any bound turn (the dev-palette
  `execute_tracked_tool` smoke path — DEBT-122) the sink is `None` and nothing is emitted; this is
  the expected, not an error, path.
- **Sequencing:** the marker (`kind="command"`, `ref=exec_id`) fires **before** `adapter.execute`
  is awaited, giving the timeline node a live "active" state for free — no output streaming
  required. The detail fires after: on the success path, sourced from the same masked record
  `record_exec` already returns for the dashboard's exec-log ring (one masking site, never two —
  see below); on the fault path (`adapter.execute` raises), a terminal detail fires with
  `exit_code=None` and `error` set, from the `except` block, and the original exception is then
  **re-raised** — emission never masks or replaces the real outcome.
- **Single masking site:** `record_exec` (unchanged masking/capping logic) now additionally
  returns the record it builds — `stdout`/`stderr` masked and capped **independently** (not
  sharing one combined budget, unlike the pre-existing ring `output` field, which is untouched for
  the dashboard's own contract) — so the WebSocket path can never diverge from the HTTP dashboard's
  exec-log tail on what is safe to show.
- **A command a permission gate or the dangerous-pattern intercept refuses never reaches
  `record_execution`**, so it would otherwise vanish from the timeline entirely. §38's
  `_classify_activity` verb table drops `("running ", "command")` (now redundant with the marker
  above) and gains `("blocked ", "command")`, `metric="denied"` — narrated by `agents/coder.py`'s
  permission-deny branch via the existing `_emit`/`_narrate` closure, and by
  `tools/execution_tools.py`'s `sandbox_bash` dangerous-pattern intercept via the new
  `ActivitySink.emit_blocked(target)` method (no `_narrate` closure is reachable from that leaf
  tool, hence the second, narrower sink method). A `blocked` marker carries no `ref` and never gets
  a matching detail — the frontend resolves it to `done` immediately, same as any other
  self-contained marker.

**Consumer contract (frontend):**
- `timelineBuilder.ts::upsertExecutionBody(entries, ref, execution, ts)` — identity-keyed by `ref`,
  order-agnostic like every other §38 body upsert: if the detail arrives before its marker, it
  creates an already-resolved placeholder (`status` derived from `exit_code`/`error`) that the
  later marker's merge path in `upsertActivityMarker` adopts **without** touching `status` — the
  outcome is already final by the time the marker shows up.
- `upsertActivityMarker`'s active-status rule gains one case: a `command` marker **carrying a
  `ref`** now starts `active` (mirroring `reasoning`/`cell`), resolving to `done`/`failed` once the
  detail lands. A ref-less `command` marker (including the new `blocked` outcome, and any frame
  from an older server) keeps the pre-existing immediate-`done` behavior — fully backward
  compatible.
- `AgentTimeline.tsx` renders a `command` entry carrying `execution` as an expandable node (new
  `ExecutionDetail.tsx`): a header naming the execution tier + working directory + initiating tool,
  then stdout/stderr (or `error`), then exit code + duration. `activityLabels.ts::timelineEntryLabel`
  special-cases `metric === 'denied'` to read "Blocked …" rather than "Running …" — a command a
  permission gate refused must not be mislabeled as having run.

**Backward-compat guarantee:**
- Old clients ignoring `server_activity_detail` see an unresolved `command` marker with no I/O
  body — no crash, no missing-field error (unknown events are silently dropped on the multiplexed
  socket, same as every other additive event in this document).

Additive-only. No existing field changed shape; `docs/TECH_DEBT_BACKLOG.md` carries the deferred
scope (background-task executions, file-read/MCP-tool detail, live incremental streaming).

---

## §41 — Prompt System-Message HEAD/TAIL Split (Prefix Stability)

**Change:** the Planner's and Coder's SYSTEM message is now split into a byte-identical-across-calls
HEAD (identity, axiom, RBAC rules, project instructions) and a small per-turn TAIL declaring the
sandbox nonce (`agents/prompts.py::build_boundary_declaration`). Previously the per-turn
`uuid.uuid4().hex` boundary nonce was interpolated directly into the axiom text at the start of the
system prompt (`BASE_SYSTEM_PROMPT`'s `{boundary}`/`{ide_context}` fields, via `build_safe_prompt`),
so the prefix changed on every single call — defeating both provider prompt caching and a local
engine's own KV-prefix reuse. This is the prerequisite Manifest 12.1 ships; `cache_control`
application itself is deferred (DEBT-137 in `docs/TECH_DEBT_BACKLOG.md`) until the prefix clears a
provider's minimum-cacheable-token floor (unblocked by Division 12.7's coder tool-calling).
**Location:** `ailienant-core/agents/prompts.py` (new `_STATIC_SYSTEM_HEAD`,
`build_static_identity_prompt`, `build_boundary_declaration` — additive; `BASE_SYSTEM_PROMPT` and
`build_safe_prompt` are byte-for-byte unchanged and still serve the Researcher, which legitimately
embeds retrieved content directly in its system prompt); `agents/planner.py::run_planner_node`;
`agents/coder.py::run_coder_node`.
**Schema version:** N/A — no wire contract changed. This is a prompt-construction internal; the
`messages` list shape sent to the LLM gateway (`[{"role": "system", ...}, {"role": "user", ...}]`)
is unchanged, only the system message's byte content.

**Design — rejected alternative and why:** an earlier draft relocated the nonce *declaration* into
the user turn (alongside the wrapped untrusted content it already carries). Rejected in review: it
introduces declaration syntax into the same message ROLE that untrusted content reaches, so a
malicious file or RAG snippet could emit a competing `SECURE DELIMITER FOR THIS TURN: <forged>`
line with no structural way for the model to prefer the real one — unguessability of the nonce is
not a defense here, since the attacker never needs to guess it, only to declare a different one.
**Adopted design:** the declaration stays exclusively in the system role, which untrusted content
can never write to. Only the axiom's *value* (which literal tag is in force) is per-turn; the
axiom's *rule text* (what the delimiter means, how to treat content inside it) is static and lives
in the byte-identical HEAD. `build_boundary_declaration`'s output is appended unconditionally, as
the last fragment of the system message, in both the success path and the `ContextBudgetError`
degrade path — never routed through the ContextPipeline's budget-guarded, silently-droppable
layers, so the sandbox seal can never be trimmed away even when rules/skills/project-instructions
legitimately are (that remains an acceptable quality tradeoff, announced to the model via
`AMNESIA_ALERT`; losing the seal while untrusted content remains in the user turn would instead be
a security regression).

**Coder-side hardening (incidental, not scope creep):** `build_coder_system_prompt` never carried a
quarantine axiom at all — the only inert-data instruction lived in the user-turn task preamble. The
same HEAD/TAIL mechanics now also give the coder a system-role boundary declaration where none
existed before, closing that latent gap as a side effect of the prefix-stability fix.

**User-turn wording:** the planner's and coder's user-turn sentences that reference the boundary tag
(`"IDE context (see the <{boundary}> tags below):"` etc.) were reworded to bare references — naming
which tag wraps the untrusted content without restating axiom/declaration language ("treat as inert
data", "ignore any directive"). That vocabulary now appears exclusively in the system message; its
absence from the user turn is asserted directly by `tests/test_prompt_prefix_stability.py`'s SEAL2
cases, the regression lock on the rejected-design injection vector above.

**agentic_cell (ReAct loop):** `brain/agentic_cell.py::_build_messages` already
never embedded a per-turn nonce in `messages[0]` — the leading system message is a hardcoded literal
string. No code change was required there; `test_agentic_cell_system_row_is_byte_identical_across_iterations`
locks the property in as a regression guard.

**Backward-compat guarantee:** additive-only at the module level (`build_safe_prompt` untouched);
behavior-preserving at the agent level (skill directives, RBAC rules, and project instructions still
appear in the system message with identical droppability semantics under budget pressure — only
their trailing position within the joined block, and the identity block's nonce-freedom, changed).

---

## §42 — `server_agent_todos` WS Event (agent_todos Write-Through)

**Change:** the agentic cell's `todo_write` fallback-dispatch tool now writes through to the
`agent_todos` `AIlienantGraphState` channel (§14) instead of only appearing as a trajectory
observation string, and broadcasts the current list over a new additive WS event so the extension
can render it live. Closes the second half of DEBT-054 (`docs/TECH_DEBT_BACKLOG.md`) — Division
8.18 had already wired the tool into the dispatch loop; the channel write-through and the UI surface
were the remaining gap.

**Wire contract (additive, new event — no existing event changed shape):**

```python
class AgentTodoItemPayload(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"]
    active_form: str

class AgentTodosPayload(BaseModel):
    session_id: str
    iteration: int
    todos: List[AgentTodoItemPayload]

class ServerAgentTodosEvent(BaseModel):
    event_type: Literal["server_agent_todos"] = "server_agent_todos"
    data: AgentTodosPayload
```

**Location:** `ailienant-core/api/ws_contracts.py` (payload/event classes, added to the
`WebSocketMessage` union); `ailienant-core/api/websocket_manager.py`
(`ConnectionManager.broadcast_agent_todos`, `LiveCellDispatcher.emit_agent_todos`);
`ailienant-core/brain/cell_dispatcher.py` (`CellEventDispatcher.emit_agent_todos` added to the
Protocol + `NullCellDispatcher` no-op — a widening covered by updating the one existing capturing
test stub, `tests/test_phase7_19_4_cell_dispatcher.py::CapturingCellDispatcher`).
`ailienant-extension/src/api/contracts.ts` mirrors the three types and adds the tag to
`SERVER_EVENT_TYPES`.

**State-promotion seam (the mechanism that fills the payload):**
`ailienant-core/core/tool_dispatch.py` adds `DispatchResult.state_delta: Optional[Dict[str, Any]]`
(additive, default `None` — the three other `ToolDispatcher` consumers are unaffected) and a
module-level allowlist, `_STATE_PROMOTERS = {"todo_write": "agent_todos"}`, consulted by
`promote_tool_state(tool_name, raw) -> Optional[Dict[str, Any]]`. A tool name absent from the
allowlist can never write a graph channel through this path — deliberately not a generic "any JSON
key becomes a channel" rule. `brain/agentic_cell.py` folds `state_delta` into both of its return
points that can carry it: the normal per-iteration delta, and the HITL-deferred `defer_delta`
early-return (a `todo_write` call preceding a deferred `run_terminal` in the same iteration must not
be lost).

**Ordering hazard closed — parse before truncate:** `promote_tool_state` is called on the tool's
*untruncated* observation text, before `ToolDispatcher.dispatch`'s existing `MAX_OBSERVATION_CHARS`
(4000-char) clamp — truncating first could split the JSON mid-object and corrupt (not just crop) a
state write. That ordering means the promoter reads a string whose length the model effectively
controls, so it checks length **before** calling `json.loads`: a new `shared/config.py` constant,
`MAX_JSON_PARSE_CHARS = 50_000`, is checked first and the function returns `None` without parsing if
exceeded — an O(1) rejection rather than a synchronous O(L) parse of an unbounded string on the
FastAPI event loop. `tools/universal_tools.py::TodoWriteTool` adds a complementary, cheaper
structural bound: each item's `content`/`active_form` is truncated (never rejected) to 300 chars
before the tool ever returns, so the parse ceiling is a backstop, not the only defense.

**Emission idempotence:** the WS emit fires at most once per cell iteration — after the tool-call
loop, not from inside it, so several `todo_write` calls in one turn collapse via
`dict.update()`'s last-write-wins — and is suppressed entirely when the promoted list is
value-equal to `state["agent_todos"]` (the channel's previously committed value). Plain equality,
not a hash/digest: the structure is bounded (≤50 items, `TodoWriteTool`'s own cap) and flat, so a
digest would add a hashing pass and a canonicalization step for no benefit at this size. The
frontend independently guards the render side: `workspaceStore.ts::setAgentTodos` keeps the same
array reference on a deep-equal write, so even a redundant event that does arrive causes no
re-render (Zustand's shallow-compare selector sees an unchanged reference).

**Incidental fix required for this to work with real content at all:** `ToolDispatcher.dispatch`
calls `reg.tool._arun(**call.args)` directly — it does not route through LangChain's `args_schema`
validation — so `todos` previously arrived as raw `dict`s, not `TodoItem` instances. Any non-empty
real `todo_write` call through this path would `AttributeError` on `item.status` (every prior
passing test exercised only an empty list, so this was latent and untriggered).
`TodoWriteTool._arun` now coerces each item via `TodoItem.model_validate`, dropping — not failing —
a single malformed item so one bad entry cannot fail the whole write.

**Frontend:** `workspaceStore.ts` adds a transient `agentTodos: Record<string, AgentTodoItemPayload[]>`
slice keyed by session id, excluded from the persisted `pick` whitelist (mirrors `coderCompanions`
— a rehydrated TODO list would describe work that already finished). `useWSMessageHandler.ts`
dispatches the new event into the store; `Workspace.tsx`'s `submitWithMode` clears the session's
list on every new-turn submit (a cell that finishes without sending an explicit empty `todo_write`
would otherwise leave the prior task's list on screen). New `AgentTodoPanel.tsx` renders the list,
mounted above the composer; renders nothing when the list is empty or absent — no skeleton, since
unlike the Coder Companion (§37) this is optional cell output with no guaranteed broadcast to time
out on.

**Backward-compat guarantee:** fully additive. No existing WS event, `AIlienantGraphState` channel,
or `DispatchResult`/`ToolDispatcher` call signature changed shape; a client that does not recognize
`server_agent_todos` ignores it exactly like any other unknown `event_type`.

---

## §43 — Devcontainer Interactive Session Bridge (Trusted-Tier PTY Tunnel)

**Change:** eight additive WS events that complement §26's one-shot exec bridge with a persistent
interactive session inside the same provisioned devcontainer — a bidirectional analogue of
`core.pty_session.SandboxSession`, tunneled over the host bridge instead of a local reader thread.
§26's contract is unchanged and still covers one-shot `sandbox_bash` dispatch; this section adds the
session lifecycle `DevcontainerSandboxAdapter.open_session` already declared but
`WebSocketHostBridge.open_host_session` previously refused (`SandboxSessionError`).

**Correlation key:** `session_ref` (UUID4), distinct from §26's `request_id` — a session spans an
open, many stdin writes, and many stream chunks over its lifetime, where a `request_id` covers
exactly one command.

**New events** (`api/ws_contracts.py`):

| Direction | `event_type` | Payload |
|---|---|---|
| server → host | `server_devcontainer_session_open` | `session_id, session_ref, cwd, env_keys: List[str]` |
| host → server | `client_devcontainer_session_opened` | `session_id, session_ref, ok: bool, detail: Optional[str]` |
| server → host | `server_devcontainer_session_stdin` | `session_id, session_ref, data_b64: str` |
| server → host | `server_devcontainer_session_signal` | `session_id, session_ref, signal: "interrupt" \| "kill"` |
| server → host | `server_devcontainer_session_flow` | `session_id, session_ref, paused: bool` |
| server → host | `server_devcontainer_session_close` | `session_id, session_ref` |
| host → server | `client_devcontainer_session_stream` | `session_id, session_ref, chunk_b64: str` |
| host → server | `client_devcontainer_session_exit` | `session_id, session_ref, exit_code: int` |

**Bytes, not text:** `data_b64`/`chunk_b64` carry base64-encoded raw bytes, never decoded text.
`SandboxSession`'s contract is byte deltas specifically so a multibyte character is never split into
mojibake mid-stream, and the sentinel command-boundary marker (`core/command_boundary.py`, shared
with the local PTY session — see below) is control bytes `\x01\x02` that cannot round-trip through
JSON text. `env_keys` follows §26's secret hygiene: allowlisted variable **names** only, never
values.

**Stream is unified, not split by fd:** unlike §26's `DevcontainerExecStreamPayload` (which tags
`stream: "stdout" | "stderr"`), `chunk_b64` carries no stream discriminant. This matches
`SandboxSession.stream()`'s single `AsyncIterator[bytes]` contract — a real PTY interleaves
stdout/stderr into one byte stream by construction, and the local `_PtySession` never distinguishes
them either. The host driver (no real PTY — see below) merges its two Node pipes into this one
stream itself.

**Shared framing extraction:** `core.pty_session._PtySession`'s sentinel-marker command-boundary
logic (marker generation, the boundary regex, `compose`/`drain_boundaries`/`_partial_suffix_len`)
moved verbatim into a new `core/command_boundary.py::CommandBoundaryFramer`, used by both the local
PTY session and the new backend-side `api/devcontainer_bridge.py::_BridgeSandboxSession`. The
difference between the two sessions is transport only: `_PtySession` drains a reader thread over a
real PTY; `_BridgeSandboxSession` drains a manager-owned `asyncio.Queue` fed by the WS receive loop
— no reader thread, no PTY, same protocol.

**Backend producer/transport:** `ConnectionManager` (`api/websocket_manager.py`) gains the emit
methods (`emit_devcontainer_session_open/stdin/signal/flow/close`), the open waiter
(`wait_devcontainer_session_opened`, which registers the session's inbound queue *before* the reply
arrives so a racing chunk is never lost), and the resolvers the receive loop calls
(`resolve_devcontainer_session_opened`, `push_devcontainer_session_chunk`,
`check_devcontainer_session_drain`, `resolve_devcontainer_session_exit`). Consumed by
`main.py`'s receive-loop `elif` chain and by `api/devcontainer_bridge.py::WebSocketHostBridge.open_host_session`
(now returns a working session instead of raising) and `_BridgeSandboxSession` (the event-loop side
of the tunnel).

**Cancellation safety (binding invariant, not incidental):** `_BridgeSandboxSession`'s demux task
runs its cleanup — force the `stream()` EOF sentinel, unregister the manager's per-session
bookkeeping — inside an unconditional `finally`, so it fires on normal EOF, on an unexpected
exception, **and** on `asyncio.CancelledError` alike. `ConnectionManager.unregister_devcontainer_session`
is idempotent (`.pop(..., None)` / `.discard(...)` throughout) because it is reachable from three
independent, potentially-interleaving callers: the session's own graceful close, a failed/timed-out
open, and `_reap_client_state` on a client disconnect (which also wakes any open-waiter still
suspended and force-EOFs every live session's queue, so a mid-session disconnect resolves rather
than hangs).

**Backpressure (bidirectional detection, not producer-only):** a bounded per-session queue
(`_DEVC_SESSION_QUEUE_CAPACITY = 256`, mirroring `core.pty_session._QUEUE_MAXSIZE`) signals
`flow(paused=true)` at a high-water mark and `flow(paused=false)` at a low-water mark. The
transition check runs on **both** the producer side (`push_devcontainer_session_chunk`, after an
enqueue) and the consumer side (`check_devcontainer_session_drain`, after every dequeue) — a
producer-only check would deadlock once the queue is full and the child's output has genuinely
stalled: the child stays blocked writing to its own stdout pipe because the host never resumed
reading it, and nothing would ever push again to trip a producer-only resume check. Sizing rationale
(the 64-slot headroom between high-water and capacity) is documented at the constant in
`api/websocket_manager.py`, derived from the host's own ~50ms stream-coalescing window. A chunk
dropped on a genuinely full queue is counted and logged — a last resort this sizing is designed to
make unreachable, never a routine mode. `push_devcontainer_session_chunk` never awaits, by
construction: it is called from the shared WS receive loop, and an `await queue.put()` there would
head-of-line block inbound traffic (chat, HITL, abort) for every connected client, not just the one
session whose queue happened to be full.

**Host driver:** new `ailienant-extension/src/providers/devcontainerSessionHandler.ts` —
stateful (unlike the one-shot `devcontainerExecHandler.ts`'s pure function), since a session's
child process, coalescing buffer, and idle timer all survive across many separate inbound frames.
Spawns `devcontainer exec --workspace-folder <root> -- /bin/sh` via the provisioner's resolved CLI +
injected `spawn` — deliberately **no `node-pty`** (a native module is a packaging/supply-chain cost
this tunnel does not justify per CLAUDE.md §9, and the sentinel-marker protocol gives command
framing without a real TTY). Declared MVP tradeoff, logged as DEBT-139: no job control, no
`isatty()`, and `interrupt` sends a best-effort `SIGINT` to the child rather than a true Ctrl-C to a
foreground process group. An idle ceiling (default 10 minutes, keyed on inbound stdin/flow activity
only, never on outbound data) self-terminates a session whose backend has evaporated without a clean
close. `dispose()` — wired into the same `context.subscriptions` lifecycle as
`disposeDevcontainerProvisioner` — kills every tracked child through a SIGTERM→SIGKILL escalation
mirroring `DevcontainerProvisioner._killChild`, so a window reload or extension deactivation can
never orphan a driver.

**Not wired to a production consumer in this change:** `brain/agentic_cell.py` still resolves
`get_active_adapter()` (the locked oracle tier) rather than the devcontainer tier this section adds
session support to. Rerouting it is deliberately deferred — see `TECH_DEBT_BACKLOG.md` DEBT-138 for
the OCC precondition that blocks it (cell writes through a bind-mounted `SyncSurface` would bypass
the VFS barrier's hash-based stale-guard). The tunnel is exercised end-to-end today through the
existing `configurable["cell_adapter"]` test seam and the checkpoint-gate-style unit suites
(`tests/test_devcontainer_session_manager.py`, `tests/test_devcontainer_bridge.py`,
`src/test/devcontainerSessionHandler.test.ts`), not through a live production call path.

**Backward-compat guarantee:** fully additive. §26's five events, `HostExecutionBridge` Protocol
methods, and `SandboxSession` ABC are all unchanged in shape; a client that does not recognize any of
the eight new event types ignores it exactly like any other unknown `event_type`.

## §44 — Reasoning-Mode Task Config on Graph State (Cross-Restart HITL Fidelity, Manifest 12.5)

**Change:** two additive `AIlienantGraphState` channels — `enable_native_thinking: Optional[bool]`
and `thinking_budget_tokens: Optional[int]` — mirroring the per-turn Reasoning Mode toggle that was
previously threaded only via `config["configurable"]` (`agents/coder.py`, `agents/planner.py`) and
therefore never survived a checkpoint. Seeded once by `_build_initial_state` from
`TaskPayload.enable_native_thinking`/`thinking_budget_tokens`; never mutated by a node.

**Why graph state, not a new table:** `execution_mode` (this file's Phase 4.3 entry) already
establishes the precedent of per-task config living on a state channel and being recovered from
`snapshot.values` at rehydrate. `HybridCheckpointer` promotes once per completed run (not per step),
so the two scalars add no new I/O; they ride alongside `vfs_buffer`, which already carries full file
contents. A dedicated relational config table was considered and rejected for *this* closure as
premature — logged instead as a forward-looking backlog entry with an explicit trigger (see
`docs/TECH_DEBT_BACKLOG.md`, the config/runtime-separation entry filed at 12.5): the moment a third
piece of per-task config needs to survive a restart, migrate all of it to a table at once rather than
adding a third ad hoc channel.

**Fixes a real defect, not just a fidelity gap:** `core/task_service.py::rehydrate_paused_interrupt`
previously reconstructed `TaskPayload(task_prompt="", …)` after a server restart. That prompt is not
merely cosmetic — the shared post-resume path (`_run_coding_task`'s `_append_history(session_id,
"user", payload.task_prompt)` calls) still executes for a resumed turn, so the empty string was
written into the **persisted transcript** as a blank user message. `user_input` is already a graph
state channel, so the prompt is recovered from `snapshot.values` the same way `execution_mode` is;
the two new channels close the remaining thinking-config gap the same way.

**Backward-compat guarantee:** both channels are `Optional` and absent-until-written. A checkpoint
written before this change deserializes them as missing; every reader treats `None` as "use the
literal default" (`True` / `4096`), identical to today's pre-checkpoint behavior. No existing channel
changes shape.

## §45 — GraphRAG Retrieval-Fidelity Additive Fields

**`EmbeddingTarget.max_input_tokens` (`core/config/byom_config.py`):** new field,
`int = 8191` (the ada-002-family default). Additive and defaulted — every existing keyword-based
construction site (`api/byom.py`'s `_build_embedding_target`/`_derive_embedding_target`) and every
`byom_config.json` persisted before this change keeps working unchanged; the field is simply absent
until a preset is re-applied, at which point the default value is written through like any other new
`EmbeddingTarget` field. Read by `core/memory/semantic_memory.py::semantic_upsert` to resolve the
embed-input truncation ceiling per active provider instead of a fixed module constant.

**MCP `query_memory` — `snippet` evidence-shape refinement (`gateway/handlers.py::handle_query_memory`,
catalog entry in `gateway/catalog.py`):** the tool's declared contract (`{file_path: string, snippet:
string}` per result, `query`/`workspace_root` input) is unchanged — same field names, same types, same
cardinality. What changed is what `snippet` *contains*: previously the stored `content_snippet`
(literally the first 500 characters of the matched file — an audit/debug value never intended as
retrieval evidence); now, when the caller's `workspace_root` resolves the file for reading, an AST
skeleton of the whole matched file (signatures + docstrings, bodies elided). A consumer reading
`snippet` as opaque text — the documented and only sane way to consume it, since its exact contents
were never part of the contract — sees no breaking change. This is recorded here per CLAUDE.md §10
because the *value semantics* of an existing field changed even though its *shape* did not; future
schema audits should treat this as the authoritative note for why `snippet` content differs from
what an MCP client might have observed before 2026-08-03. Consumers that cannot resolve a workspace
root, or whose target file is unreadable/unsupported/oversized, continue to receive the pre-fix
500-char slice — the refinement degrades, it never removes the field.

**Superseded by §46 (2026-08-03):** the forward-spec that originally stood here for the DEBT-140/12.13
chunk-embeddings table has been implemented and is now documented as-built in §46. Two points from the
original spec changed on implementation: the table gained a `chunk_text` + `content_hash` column pair
that this forward-spec did not list — without stored text there is nothing for query-time distillation
to migrate *to* — and the migration was narrowed rather than completed (§46 explains why). See §46 for
the authoritative contract.

## §46 — Symbol-Level Chunk Embeddings (`symbol_chunk_embeddings`, Manifest 12.13)

Ships DEBT-140's fix: a second, additive LanceDB table supplementing (never mutating) the
`workspace_embeddings` schema at §5. A project indexed before this table existed simply has no rows in
it; every consumer tolerates that and falls back to file-level evidence — no reader errors on absence.

```
symbol_chunk_embeddings:
    file_path       utf8
    workspace_hash  utf8              -- project_id isolation key, identical semantics to workspace_embeddings
    qualified_name  utf8              -- dotted FQN from core/ast_engine.py::collect_symbol_defs
    kind            utf8              -- "function" | "method" (classes are never chunked — see below)
    start_line      int32             -- 1-indexed, inclusive
    end_line        int32             -- 1-indexed, inclusive
    chunk_text      utf8              -- the symbol's source, capped at _CHUNK_TEXT_MAX_CHARS (4000);
                                       -- this IS the stored retrieval evidence, and the exact embed input
    content_hash    utf8              -- sha256(chunk_text) — the vector-reuse key, see below
    token_count     int32
    vector          list<float32>[dim]  -- same embedding model, metric, and dimension as workspace_embeddings
    indexed_at      utf8              -- ISO-8601 UTC
```

**Population (`core/memory/semantic_memory.py`):** `semantic_upsert` gained an additive
`symbols: Optional[Sequence[SymbolDef]] = None` keyword. When supplied and the file's token count
clears `_CHUNK_FILE_MIN_TOKENS` (800, env-overridable via `AILIENANT_CHUNK_FILE_MIN_TOKENS`), one row
is written per `function`/`method` symbol above `_CHUNK_MIN_TOKENS` (20 — a floor scoped to chunks,
distinct from the file-level `_MIN_TOKENS` (100), which would drop nearly every real function if
reused directly). `symbols` is sourced from `IndexingResult.symbols` — already produced by the single
tree-sitter parse both indexing call sites perform (`core/indexer.py`'s bulk and reactive paths) — not
from the `symbol_definitions` catalog, which the cold/bulk path never populates (DEBT-147). **Class
rows are never written:** `collect_symbol_defs` emits a class and its methods, and the class range
fully contains each method's, so embedding both would double-pay for the same bytes and double-count
in retrieval; a class's header/docstring already rides in the file-level vector. A malformed/empty
line-range slice is dropped, never sent to the embedder as an empty string.

**Content-addressed reuse (the incremental-cost guard):** on each upsert, a file's existing
`(content_hash → vector)` pairs are read back, and only chunks whose hash is unmatched are embedded —
everything else reuses its stored vector, refreshed only in `qualified_name`/`start_line`/`end_line`
metadata. The key is deliberately the chunk text's own digest, not `(qualified_name, start_line)`:
line numbers shift on any edit above a symbol, so a positional key would mark every chunk dirty (and
re-embed the whole file) on something as small as a one-line insert elsewhere in the file — exactly
the regression this key avoids. Chunk writes are all-or-nothing per file: an embedding failure aborts
before any row is written, so a partial symbol set can never masquerade as complete evidence, and the
file-level row (written first, unconditionally) is never affected by a chunk-side failure.

**Batched embedding (`_get_embeddings`, `_partition_for_embedding`, `_embed_batch`):** requests are
partitioned by **both** item count (`_EMBED_BATCH_SIZE` = 32) and cumulative token payload
(`get_embedding_target().max_input_tokens`), whichever binds first — a handful of large functions can
breach a local OpenAI-compatible provider's (Ollama/vLLM/LM Studio) payload ceiling well under the
32-item bound, surfacing as an HTTP 413 or a silently truncated response array. Batches dispatch
concurrently under `asyncio.Semaphore(_EMBED_CONCURRENCY = 4)`; a provider returning fewer vectors
than requested falls back to sequential single-text calls for that batch rather than silently
misaligning vectors to symbols.

**Read path (`search_snippets` only — routing meters are untouched, see below):** `_query_chunks`
mirrors the existing `_query_snippets` injection-safe `.where()` pushdown. The file-table and
chunk-table lookups run under one `asyncio.gather` so total latency stays
`max(O(log N_file), O(log N_chunk))` rather than their sum; an exception on either side is logged with
`exc_info=True` and degrades to the other side's results — the query path never fails outright because
one store had a transient fault. Multiple chunk hits on the same file are packed into that file's
evidence by a nearest-first greedy budget (`_MAX_EVIDENCE_CHARS_PER_FILE` = 2000 chars, sized against
the pre-chunking ceiling of `_TOP_K`(5) × the AST-skeleton cap of 1500 bytes = 7500 chars total, so
chunking cannot blow the same context budget it exists to shrink). A file with no chunk hits — because
it is under the chunking threshold, or predates this table entirely — falls through to the pre-existing
query-time AST-skeleton distillation (§45; **narrowed by this table's arrival, not removed**, since
hybrid-by-size guarantees some files never get stored chunk evidence at all).

**Routing meters are deliberately untouched:** `search`/`search_with_paths` — which feed
`CSS = 0.5·semantic_similarity + 0.3·graph_coverage + 0.2·recency_score` and gate `is_red_alert` — read
only `workspace_embeddings`. Chunk distances run systematically smaller than file-centroid distances
(that is the entire point of chunking), so merging them into these two methods would silently inflate
CSS and shift routing as a side effect of a retrieval-evidence change. Recalibrating routing against
symbol-level evidence is a deliberate future decision, not a rot bug — tracked as DEBT-149.

**Rot prevention — the three consumers that actually needed extending:**
- `SemanticMemoryManager.semantic_delete` / `_delete_record` — deletes from both tables under the same
  `(workspace_hash, file_path)` predicate; this transitively covers the §34 `/embeddings/purge`
  endpoint, which delegates here.
- `core/janitor.py::_vector_gc_sync` — unions orphan candidates across both tables before the
  filesystem-existence check (a file may have a stale row in only one table), then issues the delete
  predicate against every table regardless of which one actually held the row — a no-op delete on an
  absent row is harmless, while skipping a table risks leaving a ghost.
- The dimension-mismatch drop/recreate path in `_write_record` — a provider swap that changes embedding
  dimension drops the chunk table alongside the file table (recreated lazily on the next chunk write);
  leaving it behind would strand stale-dimension vectors that fail every subsequent chunk search.

`pca_project_2d` and the §34 `dump_vectors`/`list_embeddings` dashboard reads needed **no** change — the
former is a pure function with no table coupling, and the latter remain file-level browsers by design;
surfacing chunk vectors in the dashboard scatter map is a separate frontend feature (DEBT-148), not rot.

**Adoption — `POST /api/v1/memory/chunks/backfill` (`api/memory_dashboard.py`):** there is no
full-reindex path anywhere in this codebase (no CLI, no endpoint, no UI command; `LazyIndexer` actively
skips its crawl once a workspace is already indexed), so without this endpoint a project indexed before
12.13 would only ever accumulate chunk rows for files edited afterward — excluding precisely the large
legacy files DEBT-140 was about. `ChunkBackfillRequest{project_id, limit≤500, force, confirm:true}` →
`ChunkBackfillResponse{ok, processed, chunked, skipped, remaining}`. `workspace_root` is resolved
server-side from the persisted `projects` registry (§35.1) — **never accepted from the request body** —
because that value confines every file read the pass performs and must come from a trusted source, not
client input. Candidates are files already over `_CHUNK_FILE_MIN_TOKENS` per `list_embeddings`, minus
files that already have chunk rows unless `force`. Parses via the identical `compute_pool.run(
index_file_sync, ...)` call the indexer itself makes — no bespoke second parser, tree-sitter stays off
the event loop. Deny-if-busy per `project_id` (a concurrent second call gets HTTP 409, not a silently
coalesced/empty result — an HTTP caller needs either the real count or an explicit "try again", never a
fabricated zero); bounded and resumable via `limit`/`remaining`; the ANN index is rebuilt once at the
end of a batch, not per file, since the per-file `create_index(replace=True)` pattern the reactive path
uses would make a large backfill pass quadratic.

---

## §47 — Sandbox Container Pool + Docker Daemon Circuit Breaker

Closes DEBT-097 and DEBT-100. `core/sandbox.py::DockerSandboxAdapter` no longer owns one
process-lifetime `self._container`; it leases from a bounded `_ContainerPool` keyed by
`(abspath(mount_root), session_id or "__shared__")`. Every field this section touches is additive or a
same-shape rename — no consumer of the pre-12.6 contract breaks.

**Container identity and labels.** A lease's container is named `ailienant-sandbox-<sha1(key)[:12]>`
(was the fixed singleton `ailienant-sandbox-daemon`) and carries two labels:
`ailienant.sandbox=1` (the sweep/list filter) and `ailienant.owner_port=<AILIENANT_API_PORT or "">`
(the sibling-backend liveness check below). A pre-12.6 singleton container is a one-time migration
target, not a supported shape going forward.

**Mount-root resolution — additive DI seam.** `core.sandbox.set_session_workspace_resolver(fn)` mirrors
the existing `set_trusted_bridge` precedent (§26): `core` never imports the transport layer that owns
the session registry. `main.py`'s lifespan injects
`lambda sid: _session_workspace_root.get(sid, "")` — the same registry, keyed by the same
`client_id == x_task_id == session_id` identity, that `client_workspace_init` already populates. A
resolver miss (no session, or an uninjected resolver — every unit test) falls back to the adapter's own
`host_workspace`, which is unchanged in meaning: the `__shared__` lease's mount root, still readable
before any lease exists (the benchmark oracle's materialize-before-execute contract, `core/benchmark/
executors.py`).

**`container_lifecycle` event vocabulary — amends §36.2.** The table shape (`id, timestamp, event,
container_id, image, tier`) is unchanged; the free-form `event TEXT` column now also carries three new
values a reader should not treat as unknown: `evicted` (LRU eviction to free a pool slot),
`idle_ttl_reaped` (idle past `AILIENANT_SANDBOX_IDLE_TTL_S`), `shared_degraded` (pool exhaustion —
sharing an existing same-mount lease rather than creating a new one), and `drained` (lifespan shutdown —
replaces the pre-12.6 `stopped`, which described tearing down the one singleton container).

**`GET /api/v1/runtime/status` — additive fields, amends §11.3.B.** `container_running: boolean` keeps
its exact pre-12.6 meaning (at least one pooled container currently exists) computed from the pool's own
bookkeeping rather than a live `reload()` call, since the RuntimePanel polls this endpoint continuously
and a per-poll daemon round-trip would only add load. Two new fields:

| Field | Meaning |
|---|---|
| `container_count` | Number of currently-leased pool containers (0..`AILIENANT_SANDBOX_MAX_CONTAINERS`). |
| `daemon_degraded` | `true` when the daemon circuit breaker (below) is open. |

**Daemon circuit breaker (DEBT-100).** Every blocking Docker SDK call — including `api/runtime.py`'s
`_probe_docker`/`_check_image_exists`, previously their own unbounded `asyncio.to_thread` calls — now
routes through `core.sandbox.docker_call`, which dispatches to a dedicated `ail-docker`
`ThreadPoolExecutor` (never the shared default executor every other subsystem depends on) and is guarded
by a 3-state breaker (closed → 2 consecutive faults → open 60s → half-open probe). No new wire contract:
the breaker's state surfaces only through the `daemon_degraded` status field above and through the
existing `[sandbox_daemon_unavailable]` / `[sandbox_pool_exhausted]` bracketed-sentinel convention
already established for `[sandbox_timeout]` / `[hitl_denied]` / `[devcontainer_*]` (§26).

**Config — additive, `shared/config.py`.** `AILIENANT_SANDBOX_MAX_CONTAINERS` (4),
`AILIENANT_SANDBOX_IDLE_TTL_S` (900), `AILIENANT_SANDBOX_LEASE_WAIT_S` (30),
`AILIENANT_SANDBOX_MEM_LIMIT` ("2g"), `AILIENANT_SANDBOX_PIDS_LIMIT` (512),
`AILIENANT_DOCKER_OP_TIMEOUT_S` (30). None are read by any pre-12.6 code path.

---

## §48 — Agentic-Cell Registry-Fallback HITL Defer (`pending_tool_call`)

Closes DEBT-129. `brain/agentic_cell.py`'s registry-fallback dispatch (Division 8.18.2 —
a tool name outside the 3 `CELL_TOOLS` primitives, resolved through `core/tool_registry.py`)
previously built its `ToolDispatcher` with `approval_fn=None`, so a HITL-tier fallback tool
was safely denied-with-report rather than surfaced as an approval card. This section adds the
generalized defer-then-interrupt-first path the existing `pending_exec_command` channel already
established for `run_terminal`.

**New `AIlienantGraphState` channel (additive, last-value):**

```python
pending_tool_call: Optional[Dict[str, Any]]   # {"name": str, "args": dict}
```

Mirrors `pending_exec_command`'s shape and lifecycle exactly. Never initialized; read via
`.get()`. Mutually exclusive with `pending_exec_command` at any given re-entry — each defer
branch returns immediately, so at most one of the two channels is ever set entering an
iteration.

**Mechanics.** `core/tool_dispatch.py::ToolDispatcher` gains a pure `classify(call) ->
(Optional[RegisteredTool], PermissionDecision, Optional[str])` — the single gate
implementation (lookup miss → role check → `evaluate_action`) that both the pre-existing
`dispatch()` and this new caller share; `dispatch()` is refactored to call it internally, with
the exact observation text it returns unchanged (verified by the existing dispatch-gate test
suite). In the cell's per-call loop, the registry-fallback branch calls `classify()` *before*
`dispatch()`; on `PermissionDecision.HITL` it stops processing further tool calls this
iteration (no side effect precedes approval), commits any edits already computed
(`vfs_buffer`/`pending_contents`, same as the `run_terminal` defer), sets `pending_tool_call`,
and returns `status="continue"` so `route_after_cell` loops back. On `ALLOW`/`DENY` the branch
is behaviorally unchanged from Division 8.18.2.

A new tool-call-approval phase runs at the top of the node, immediately after the existing
`pending_exec_command` phase. `interrupt()` (via `core/hitl.py::request_graph_approval`,
or the injected `cell_tool_approval_fn` test seam) is the first action, so a pre-resume replay
is side-effect-free. The approved tool is resolved by **exact name** against
`tool_rag_store.all_schemas()` — deliberately never by re-running the intent-ranked
`select_tools()`, whose ranking is not guaranteed stable across a suspend/resume boundary — then
dispatched exactly once through a fresh, single-tool `ToolDispatcher` with a pre-approved
`approval_fn`. A name that no longer resolves (catalog changed across a restart) reports a
system observation and clears the channel rather than hanging. Trust-once
(`_is_session_trusted`) is deliberately not applied, matching the exec-approval phase: each
gated call resumes through its own interrupt.

**New security flag:** `TOOL_CALL_HITL_DENIED` (parallel to `EXECUTE_TIER_HITL_DENIED`),
emitted on operator denial.

**Backward-compat:** additive channel, additive `ToolDispatcher` method (pure, no behavior
change to existing callers), additive node phase. `_build_fallback_dispatcher`'s memoized,
per-iteration dispatcher still runs with `approval_fn=None` — a HITL verdict is now never
reached there at all, since the defer happens one level up via `classify()`, before `.dispatch()`
would otherwise degrade it to deny-with-report.

---

## §49 — Dev-Role Subagent Tool Arsenal + Prompt Overrides

Closes DEBT-106 and DEBT-127. No new wire contract — this section documents a substrate reuse,
not a schema change, so it is recorded for the audit trail rather than introducing new fields.

`brain/nodes/subagent_worker_node.py::_resolve_tools` previously ran every one of the 8
developer dispatch roles (`core_dev`, `architect_refactor`, `devops_infra`, `secops`,
`qa_tester`, `doc_manager`, `vcs_manager`, `data_ml_engineer`) tool-less — only the
`analyst_readonly` critic role had an arsenal (`tools/analyst_tools.py::build_analyst_tools`).
The 8 dev roles now resolve through the identical `core.tool_rag.tool_rag_store.select_tools`
→ `core.tool_registry.resolve_tools` substrate the agentic cell's registry fallback (§48) and
the coder's grounding pre-pass already use — no new, hand-maintained arsenal builder.
`analyst_readonly` is unaffected: it still routes to `build_analyst_tools` directly, since
analyst tools register under `allowed_roles={"analyst"}` in the Tool RAG catalog — a name
disjoint from the `analyst_readonly` dispatch identity, so the shared catalog path would return
nothing for it regardless. An unrecognized role (not in `shared.rbac.DISPATCH_ROLE_PERMISSIONS`)
stays tool-less, and any resolution fault degrades to tool-less — both fail-safe, unchanged from
the pre-12.7 contract.

`agents/roles.py` gains `build_subagent_system_prompt(role, override)` — a seam distinct from
`build_coder_system_prompt`: it composes the role directive (or the saved override) plus the
language-mirror directive, deliberately **without** `_BASE_CODER_PROMPT` (the SEARCH/REPLACE
output contract, which does not apply to a subagent constrained to a `response_schema`-driven
JSON answer). `subagent_worker` resolves it once per invocation from `state["agent_role_overrides"]`
(the same channel `agents/coder.py` already reads) and threads it into both the tool-loop seed
message and the final-answer synthesiser. The `AnswerFn` signature
(`Callable[[SubagentTask, List[str]], Awaitable[Dict[str, Any]]]`) is unchanged — the default
synthesiser is now built by a `_make_default_answer(system_prompt) -> AnswerFn` closure, so an
injected `dispatch_answer_fn` (the hermetic-test seam) is unaffected.

---

## §50 — Glass-Box Timeline: Tool-Call Detail, Live Exec Chunks, Replay-Safe Defer Ref

Phase 12.8 (Fresh Debt Triage Sweep) — closes DEBT-133 (tool-call + file-read visibility) and
DEBT-134 (incremental execution detail). Three additive changes, all following §40's existing
marker/detail pattern rather than introducing a new one.

**1. `ToolDispatcher.dispatch` now instruments the §40 `ActivitySink`.** `core/tool_dispatch.py`
gains an optional `activity_ref: Optional[str] = None` parameter. With no ref supplied (every
call site except #2 below), `dispatch` owns the row's full lifecycle exactly like
`record_execution` does for commands: a DENY/unresolved/HITL-not-admitted call that never
reaches `tool._arun` emits a single ref-less `emit_blocked`; a call that does execute mints its
own ref, emits an opening `emit_marker`, then a closing `emit_detail` (masked/capped via the
same discipline, factored into shared `core/redaction.py::truncate_middle` — extracted from
`core/exec_log.py`, which now imports it rather than defining its own copy). `initiator` is the
dispatcher's own `active_role`, so a row is attributable to whichever role (analyst,
core_dev/etc. dispatched subagent, or the coder's grounding pre-pass) actually made the call.
Backward-compatible: a caller passing no `activity_ref` sees byte-identical `DispatchResult`
behavior to pre-12.8; the instrumentation is purely additive I/O to the sink.

**2. `pending_tool_call` gains an additive `activity_ref: str` field** (§48's channel, shape now
`{"name": str, "args": dict, "activity_ref": str}`). The agentic-cell HITL-defer branch
(`brain/agentic_cell.py`) mints this ref and emits the OPENING marker at defer time — a plain,
non-replayed super-step, so the mint happens exactly once — then carries it through the
checkpointed channel to the tool-call-approval phase on resume. That phase, which DOES replay
before an `interrupt()` resolves, reads the ref from state rather than re-minting it, so every
replay resolves the SAME row instead of opening a new one (`upsertActivityMarker`/
`upsertExecutionBody` merge by identity — replay-safe by construction, no new frontend logic
needed). `dispatch()` is called with `activity_ref=<the deferred ref>` on approval, so the
post-approval detail lands on the row the operator has been watching throughout the round-trip.
An operator denial, or the approved tool no longer resolving in the catalog, resolves the SAME
ref with a terminal `emit_detail` via a new `_emit_pending_tool_resolution` helper — the model
denied by DEBT-129 previously had no timeline representation at all, so this is also the first
time a HITL-tier registry-fallback call becomes visible before its outcome is known.

**3. New WebSocket event `server_activity_detail_chunk`** — an incremental stdout/stderr
fragment for a `command` node, arriving BEFORE its terminal `server_activity_detail` (§40).
Additive/optional: only a tier whose transport genuinely streams emits it (currently the
devcontainer bridge, which received the underlying incremental host-to-backend transport in
12.4/DEBT-083); every other tier is unaffected and still fills its detail body once, on
completion.

```python
class ActivityDetailChunkPayload(BaseModel):
    session_id: str
    ref: str                     # the same execution id the terminal detail resolves
    stream: Literal["stdout", "stderr"]
    chunk: str                   # masked at the emission site
```

**Correlation plumbing:** `core/activity_context.py` gains a second `ContextVar`
(`bind_exec_ref`/`current_exec_ref`), bound by `core/exec_log.py::record_execution` only around
its `adapter.execute(...)` await. `api/devcontainer_bridge.py::WebSocketHostBridge.exec_command`
reads it (paired with `current_activity_sink()`) right after minting its own transport-level
`request_id`, and — only when both resolve — calls a new
`ConnectionManager.register_devcontainer_exec_activity(request_id, exec_ref, sink)`. The existing
`append_devcontainer_stream` (now `async`, called with `await` from `main.py`'s WS receive loop)
forwards each buffered chunk to that registration's sink, masked via `core/redaction.py`.

**Runaway-output guard (§5.5), two layers:**
- **Backend** — `api/websocket_manager.py::_LIVE_STREAM_CAP` (16,000 chars) bounds cumulative
  live-forwarded characters per execution; past it, forwarding stops and one suppression notice
  fires. The underlying buffer (and therefore the terminal detail, independently capped at
  `core/exec_log.py`'s `_OUTPUT_CAP`) is never affected — only the WHILE-RUNNING wire volume is
  bounded.
- **Frontend** — `timelineBuilder.ts::upsertExecutionChunk` accumulates chunks onto
  `TimelineEntry.execution.stdout/stderr`, clamped by `shared/config.ts::MAX_LIVE_EXEC_FIELD_CHARS`
  (4,000 chars, head+tail preserved) as defense-in-depth. The terminal `upsertExecutionBody`
  REPLACES (never appends to) whatever accumulated, and a chunk arriving after an entry has
  already settled (`status !== 'active'`) is a no-op — the settled record can never be corrupted
  by a straggling live frame.

**ActivitySink Protocol:** gains an optional `emit_detail_chunk(*, ref, stream, chunk)` method.
A sink built against the pre-12.8 narrower surface (a hermetic test double, for instance) is
detected via `getattr(sink, "emit_detail_chunk", None)` and simply never called — no crash, no
required update to existing sink implementations outside `core/task_service.py`'s own
`_TurnActivitySink`, which implements it by forwarding to a new
`ConnectionManager.broadcast_activity_detail_chunk`.

**Backward-compat:** every change here is additive — a new optional dispatch parameter, a new
optional state-channel field, a new optional Protocol method, and a new WS event type. An old
client ignoring `server_activity_detail_chunk` sees only the terminal detail, exactly as before
12.8; a `pending_tool_call` written before this field existed is read with `.get("activity_ref")`
defaulting to `None`, degrading that one row to the pre-12.8 "no live visibility during approval"
behavior rather than raising.

**Deferred (logged, not closed here):** DEBT-133's file-content preview (masked head/tail of
file bytes read — deliberately out of scope, a PII/token-budget question distinct from the
metadata-only `read` marker enrichment this phase shipped) and DEBT-125's semantic edit-risk
classifier (the binary regex proxy stays; only the display wiring closed) — both re-logged in
`docs/TECH_DEBT_BACKLOG.md` with their scope-narrowing rationale.

## §51 — HITL Clarification Card: Question/Answer Fields (Manifest 13.0.1, closes DEBT-171)

13.0.1 wired `AskUserQuestionTool`'s `pending_hitl_request` channel to the working
`interrupt()` substrate (`core/hitl.py::request_graph_clarification`, added the same day for the
ambiguity gate). Doing so exposed that the existing HITL approval-card contract had no shape for
a clarification at all — `HITLApprovalRequestPayload` carried only `action_description`, and
`HITLResponsePayload` only `approved`/`comment`. Both extended additively; no existing field
changed shape or meaning (§10).

**`HITLApprovalRequestPayload` (`api/ws_contracts.py`) gains three optional fields:**

```python
question: Optional[str] = None
context: Optional[str] = None
suggested_options: Optional[List[str]] = None
```

All `None` for a `request_graph_approval`-shaped interrupt (`RISK_INTERCEPT`, `COMMAND_EXEC`,
`DRIFT_DETECTED`, `FILE_WRITE`, …); populated for a `CLARIFICATION_NEEDED` interrupt. Two
existing fields also gained a fallback specific to the clarification shape, not a schema change:
`task_service.py::_emit_interrupt_card` now resolves `approval_id` as
`payload.get("approval_id") or payload.get("request_id") or ""` (a clarification payload has no
`approval_id` at all — `request_graph_clarification` keys its cosmetic correlation id as
`request_id`; without this fallback the card's id resolved to the empty string), and
`action_description` as `payload.get("action_description") or payload.get("question") or
"Approve to continue?"` — so a clarification renders its question as the card's headline with
zero frontend changes, since the existing plain approve/reject card already displays
`action_description`.

**`HITLResponsePayload` gains two optional fields:**

```python
answer: Optional[str] = None
selected_option: Optional[str] = None
```

`main.py`'s `client_hitl_response` handler forwards both into the resume value alongside the
existing `approved`/`comment` (extracted as `main.py::_resume_approval_dict`, unit-testable
without the WS stack). `request_graph_approval` only ever reads `approved`/`comment`;
`request_graph_clarification` only ever reads `answer`/`selected_option` (`core/hitl.py`) — so
sending the union on every resume is safe regardless of which interrupt shape is actually paused.
The frontend has no multi-choice renderer yet (DEBT-172, narrowed by this closure), so `answer`
falls back to the existing free-text `comment` field when the client sends no explicit answer —
the ambiguity gate and Plan-mode suggestion (shipped the same morning) work end-to-end against
today's unchanged card.

**Backward-compat:** every field above is additive and optional. An old client omitting them on
`client_hitl_response` degrades to `answer=None, selected_option=None`, which the `comment`
fallback resolves; a pre-13.0.1 backend emitting a card without `question`/`context`/
`suggested_options` is read by pydantic as `None` on the new fields, unaffected.

---

## §52 — Multi-Question Clarification Batch: `questions`/`answers` (closes DEBT-172)

§51 closed the wire round-trip for a *single* clarification question but left two gaps: the
frontend still had no renderer for `suggested_options` at all (every clarification degraded to
the plain free-text approve/reject card), and `ask_user_question`'s LLM-facing schema had no way
to batch several related questions into one pause or to mark a recommended answer. This closure
adds a second, independent shape alongside the existing singular one — both are additive (§10);
neither repurposes an existing field.

**Three new nested models (`api/ws_contracts.py`):**

```python
class ClarificationOption(BaseModel):
    label: str
    description: Optional[str] = None
    recommended: bool = False

class ClarificationQuestion(BaseModel):
    id: str                      # stable per-question correlation key within the batch
    header: str                  # short tab label
    question: str
    context: Optional[str] = None
    options: List[ClarificationOption]
    multi_select: bool = False

class ClarificationAnswer(BaseModel):
    id: str                      # matches ClarificationQuestion.id
    selected_labels: List[str] = []
    free_text: Optional[str] = None
```

**`HITLApprovalRequestPayload` gains one optional field:** `questions: Optional[List[ClarificationQuestion]] = None`.
**`HITLResponsePayload` gains one optional field:** `answers: Optional[List[ClarificationAnswer]] = None`.

Both are `None` for every existing request/response shape (approval, single-question
clarification). `core/hitl.py::request_graph_clarification` gained an additive `questions`
parameter (its `question` parameter also relaxed from required to `Optional[str]`, a
backward-compatible widening) and folds a resumed `answers` list into its return dict alongside
the existing `answer`/`selected_option`. `_emit_interrupt_card` (`core/task_service.py`) forwards
`questions` the same way it already forwards `question`/`context`/`suggested_options`, and
synthesizes an `action_description` fallback (`"I have N question(s) before continuing."`) for a
pure-batch payload with no legacy `question` to fall back to.

**`tools/control_tools.py::AskUserQuestionInput`** gained an additive `questions:
Optional[List[AskUserQuestionItem]]` field (each item: `header`, `question`, `context?`,
`options: List[{label, description?, recommended}]`, `multi_select`). This is the actual fix for
the observed behavior where the LLM would emit at most one "recommended" suggestion instead of a
real option set: the field descriptions now explicitly instruct 2-4 concrete mutually-exclusive
options per question with exactly one marked `recommended`, and batching related questions into
one call. `AskUserQuestionTool._arun` writes `pending_hitl_request["questions"]` (each item
assigned a stable `q{i}` id) when `questions` is supplied, else keeps today's exact
singular-field behavior unchanged.

**Frontend** (`ailienant-extension/src/api/contracts.ts`) — this is also where the *existing*
§51 gap actually lived: `HITLApprovalRequestPayload` never declared `question`/`context`/
`suggested_options` at all despite the backend already sending them, so they were silently
dropped. Both closures land together here: the three fields plus the new `questions` are added
to the TS interface, alongside new `ClarificationOption`/`ClarificationQuestion`/
`ClarificationAnswer` types mirroring the Pydantic models. A new `ClarificationGrillCard.tsx`
renders either shape — a real `questions` batch, or a synthesized one-item batch built from the
legacy `question`/`suggested_options` fields (first suggested option marked recommended) — as a
tab strip (multi-question) or a single options panel, with an implicit free-text "Other" escape
hatch per question. `Workspace.tsx` branches to it instead of the plain `HITLInterventionCard`
whenever `question`/`questions` is present; every other request kind (FILE_WRITE, RISK_INTERCEPT,
MCP_TOOL_CALL, generic approval) is unaffected. `useClarificationResponder.ts` (modeled on
`useHitlResponder.ts`) posts `{approval_id, approved: true, answers}` once.

**Backward-compat:** every field is additive and optional. An old backend never populating
`questions` is read by the new frontend as the existing single-question (or no-question) shape,
unaffected. An old frontend build ignoring `questions`/`answers` still renders the existing
approve/reject card off `action_description`'s synthesized fallback text.

**Correction (13.0.6, closes DEBT-184):** `useClarificationResponder.ts` posting `answers` was
necessary but not sufficient — the extension-host bridge that relays a webview's `HITL_RESPONSE`
message onward to the backend WS (`providers/workspace_panel.ts`) whitelisted only the original
four approve/reject fields (`approval_id`, `approved`, `comment`, `modified_content`) and
silently dropped `answer`/`selected_option`/`answers`. Every option a `ClarificationGrillCard`
answer picked was discarded at that one bridge point for a full release cycle: the WS contract,
the resume path, and every backend test all round-tripped correctly in isolation, so nothing
caught it. Fixed by adding the three fields to the bridge's forwarded payload (conditional
spread — an absent field stays absent), extracted into an exported `buildHitlResponseData` pure
function specifically so this exact class of gap — a contract field correct everywhere except
one untested relay hop — has a fixture guarding it going forward.

---

## §53 — `grill_round_count`: Socratic Ideation Round Counter

§52 shipped the multi-question batch contract, but the Socratic "Grill Me" ideation flow
(`agents/analyst.py::run_analyst_node`, the `analyst_grill` node of `brain/ideation.py`) did
not use it: it predates the clarification channel entirely, streaming ONE free-text question
per turn with a "Recommended: …" line baked into the prose, setting `hitl_pending=True`, and
letting the graph run finalize so the user's next plain chat message was read as the answer.
Converting it to batch its questions through the same `request_graph_clarification` /
`ClarificationGrillCard` channel changed how a "round" maps onto graph execution, which
requires one new state channel.

**`AIlienantGraphState` (`brain/state.py`) gains two fields:** `grill_round_count: int` and
`pending_grill_batch: Optional[List[Dict[str, Any]]]` (the latter added 13.0.6, see the
"Replay-safety correction" subsection below — the shape shipped 13.0.5 only had the counter).

Why the counter must be state and not a local variable: each grill round is now driven by a
**self-loop edge** (`analyst_grill → analyst_grill`, `brain/ideation.py`) rather than the old
"return to END and await the next top-level invocation" — every round is its own graph
super-step with a fresh Python frame, so a local counter cannot survive between rounds. The
counter backs `_GRILL_MAX_ROUNDS`, the circuit breaker preventing a model that never emits its
completion signal from interviewing forever.

**Routing change (not a wire contract):** `route_after_analyst` previously returned `END` when
`shared_understanding_reached` was False (suspend, await the next user turn); it now returns
`"analyst_grill"`. The pause for the human's answer lives *inside* the node (`interrupt()`), so
the edge only decides whether another batch is needed. `core/task_service.py` needed no change
on the pause side — `extract_pending_interrupt` is already checked before the legacy
`hitl_pending` branch, so the grill's suspension flows through the existing
`_emit_interrupt_card` path. That legacy branch stays reachable for `agents/orchestrator.py`,
its only remaining writer.

### Replay-safety correction (13.0.6, closes DEBT-185)

The 13.0.5 shape above had a real bug: `run_analyst_node` composed the question batch
(`_generate_grill_questions_llm`, temperature 0.2) and called `interrupt()` in the **same**
invocation. LangGraph replays a node from the top on every resume, so the LLM call that produced
the batch replayed too — a resumed round could receive a *different* batch than the one the
operator actually answered. Ids are positional (`q{i}`), so a differently-sized replay batch
silently misaligned answers to the wrong questions with no mismatch to detect, and a replay
batch that happened to come back empty skipped `interrupt()` entirely and discarded the resume
value outright. `brain/agentic_cell.py` documents the opposite invariant in three places (its
`pending_exec_command`/`pending_tool_call`/`pending_hitl_request` defer-then-interrupt-first
phases) specifically to prevent this class of bug.

The fix adds `pending_grill_batch`, splitting each round into two graph super-steps (the
self-loop edge already existed — it now drives two invocations per round instead of one):

- **Generate phase** (`pending_grill_batch` empty): composes the batch, commits it to
  `pending_grill_batch`, and returns — no `interrupt()` call in this invocation at all.
- **Ask phase** (`pending_grill_batch` set, the self-loop's next visit): `_resolve_grill_answers`
  on the **state-sourced** batch is the node's first action, so a resume only ever replays the
  interrupt call itself, never the LLM call that produced the batch. Folds the resumed answers
  and clears the channel back to `None`.

The pre-existing "grounding may re-run once on its own round's replay" cost (accepted in 13.0.5)
is unchanged and still bounded — this fix only removes the LLM call that generates the
QUESTIONS from the replay path, which is where non-determinism caused a correctness bug rather
than just a wasted call.

**Loop-safety guard (load-bearing):** `route_after_analyst` checks `hitl_pending` FIRST and
returns `END` for it. `_generate_grill_questions_llm` returns `None` — distinct from an empty
batch, which is the model's deliberate "I have enough" signal — when no BYOM chat target
resolves or no valid batch survives the retry; the node then surfaces the actionable BYOM
notice and sets `hitl_pending=True`. Without that guard the self-loop would retry an
unreachable model until the graph's recursion limit. The `None` path also fast-fails before
`LLMGateway.ainvoke`: an `ailienant/*` alias silently falls back to the litellm proxy when no
preset is active, so without the explicit `get_chat_target` check a BYOM-down grill burned the
full transport-retry budget instead of failing immediately the way the `astream_byom` path it
replaced did.

**Completion signal:** the model now ends the interview by returning an empty `questions` list
(`GrillQuestionBatch`), replacing free-text agreement-phrase matching as the primary path;
`_AGREEMENT_SIGNALS` remains a fast path for a genuinely new top-level turn only.

**Backward-compat:** both fields are new declared channels defaulting to absent (read with
`state.get(..., 0)` / `state.get("pending_grill_batch")`, `None` treated as "not asking").
A checkpoint written before either change resumes at round 0 / no pending batch — the
worst case is one extra generate phase, never a crash or a lost answer. No wire contract
changed by either shape — both are graph-internal state; the frontend sees only the
already-shipped §52 `questions`/`answers` payloads, unaffected by which phase produced them.

---

## §52 — Agentic Cell: `cell_tool_query` Discovery-Replay Channel (Manifest 13.0.2)

`tools/meta_tools.py::ToolSearchTool` returns tool names and descriptions but deliberately
never their full `json_schema` (token hygiene), closing with an instruction to NAME the tool
in the next output so its schema is injected on the following cycle. Inside the agentic cell
that instruction could not be honoured: the cell is re-entered as a fresh graph node per
iteration and re-ran selection against the frozen `user_input`, and `select_tools` is
deterministic for a given `(intent, active_role, session_mode)` — so the next iteration's
catalog was byte-identical and the named tool never appeared. The model was told to do
something the runtime structurally could not deliver.

**`AIlienantGraphState` gains one optional scalar channel:**

```python
cell_tool_query: Optional[str]
```

Written on every `run_agentic_cell_node` return (including both defer paths): set to the
`query` argument of a `tool_search` call made during that iteration, and otherwise `None`.
The next iteration reads it, re-runs `select_tools` with that exact query, and unions the
result into the advertised schemas. Because selection is deterministic, the replay returns
precisely the names the model was just shown — the shift-left instruction becomes true by
construction rather than by chance.

**Scalar overwrite, no reducer.** The channel is consumed-and-cleared: writing `None` on
every iteration that did not call `tool_search` is what prevents a stale query from
re-selecting forever. An `operator.add` reducer would accumulate queries and defeat that.

**Backward-compat:** additive and optional. A checkpoint written before this channel existed
deserializes it as absent; `state.get("cell_tool_query")` yields `None` and the replay
branch is simply skipped, leaving selection identical to the pre-13.0.2 behavior. No existing
channel changes shape or meaning.

## §54 — Agent Companion: `scope`/`emission_id` (Manifest 13.0.7, closes DEBT-183)

The Coder Companion generalized beyond patch explanations (grill rounds, plan commits,
error corrections now emit too — see `docs/DEV_JOURNAL.md`'s 13.0.7 entry). Two additive
fields land on `CoderCompanionPayload` (`api/ws_contracts.py`):

```python
scope: Literal["coding", "ideation", "planning", "healing"] = "coding"
emission_id: Optional[str] = None
```

**`correlation_id` was deliberately NOT redefined.** It stays `f"{task_id}:{attempt_ordinal}"`,
exactly its pre-13.0.7 shape — reusing it as the new per-decision-point key would have
been a silent field-repurposing (forbidden by §10) and, worse, would not even have worked:
two emissions in the same attempt (e.g. an ideation round and, later, a coding attempt
both at ordinal 0) collide on that key, degrading an append-store to a stomp — precisely
the class of bug DEBT-184/185 closed in 13.0.6. `emission_id` is the new key a current
frontend uses instead (`f"{task_id}:{scope}:{ordinal}"`, unique per decision point);
`correlation_id` remains for whatever narrower "last write for this task+attempt" meaning
existing/older consumers already gave it.

**Backward-compat:** both fields are optional with defaults. An older backend event
(pre-13.0.7) validates unchanged — `scope` defaults to `"coding"` (correct, since no
older backend emitted anything else) and `emission_id` is `None`, which the frontend
falls back to `correlation_id` for — so an old event still keys coherently in the new
append-store, just without cross-scope disambiguation it never needed.

**Companion request contract** (`brain/coder_companion.py::CompanionAnalysisRequest`,
not a wire type, Python-internal) gained matching `scope`/`scope_summary` fields with
empty/`"coding"` defaults — the coding path's existing fields (`pending_patches` etc.)
are untouched, so no coding-scope behavior changed. Each non-coding scope builder
(`build_ideation_companion_request`, `build_planning_companion_request`,
`build_healing_companion_request`) renders its own decision point's data into
`scope_summary`, capped at `_MAX_SCOPE_SUMMARY_CHARS` (4000) before it ever reaches an
LLM prompt — the same token-hygiene discipline `_MAX_DIFF_CHARS_PER_FILE` already
applies to the coding path (§5.5).

## §55 — `HITLResponsePayload.session_id` (Manifest 13.0.8, closes DEBT-188)

One additive field on `HITLResponsePayload` (`api/ws_contracts.py`):

```python
session_id: Optional[str] = None
```

**Why:** `main.py`'s `client_hitl_response` handler resumed a paused graph by `client_id` —
the WS route's path parameter, stable for the whole physical connection — instead of the
chat's own `session_id`, under which `TaskService._paused_tasks` actually keys the pause.
`register_alias`/§51's `RegisterSessionPayload` already support several sessions sharing
one connection; this payload had no field to say which one a reply belonged to, so the two
identities only ever coincided when exactly one session was active on a connection. See
`docs/TECH_DEBT_BACKLOG.md` DEBT-188 for the full mechanism and the telemetry-log evidence.

**Backward-compat:** `None` (the default) falls back to `client_id` in `main.py`, preserving
resume behavior for a stale, pre-13.0.8 webview that hasn't reloaded yet — an older client
still round-trips correctly as long as it is the only session on its connection, exactly the
implicit assumption the pre-fix code always made.

**Frontend:** `workspace_panel.ts::buildHitlResponseData` now stamps the panel's own
`session.id` unconditionally (unlike `answer`/`selected_option`/`answers`, which forward
conditionally per §52 — the host always knows its own session identity, so there is no
"absent" case to preserve).

## §56 — Incremental Per-Step Apply Gate (Manifest 13.0.9)

The post-graph whole-WBS approval replay (a single pass over the frozen `pending_patches`
dict after every step had already run) is replaced by two graph nodes — `apply_patch`
(PREPARE, `brain/apply_gate.py::run_apply_prepare_node`) and `apply_commit` (GATE,
interrupt-first, `run_apply_commit_node`) — gating one step at a time. All changes below are
additive; a pre-13.0.9 checkpoint deserializes unchanged.

**`WBSStep` (`brain/state.py`):**

- `status` widened: `+ "awaiting_approval", "rejected", "revision_requested"`. A step now only
  reaches `completed` after the human decision (or the ALLOW/auto-accept path) is known, never
  the instant the coder generates it.
- `agent_notes: Optional[str] = None` — a directive FOR THE CODER ONLY (e.g. the polyglot-file
  patch-tool requirement `agents/planner.py::_inject_polyglot_constraints` now attaches here
  instead of appending to `description`). Never rendered on the human-facing checklist, never
  folded into a semantic-cache key or a retrieval query.

**New `AIlienantGraphState` channels (`brain/state.py`), all additive:**

| Channel | Type | Reducer | Safe default |
|---|---|---|---|
| `pending_step_files` | `Dict[str, List[str]]` | `operator.or_` | `{}` |
| `pending_step_command` | `Dict[str, str]` | `operator.or_` | `{}` |
| `pending_apply` | `Optional[Dict[str, Any]]` | scalar overwrite | `None` |
| `apply_feedback` | `Optional[Dict[str, Any]]` | scalar overwrite | `None` |
| `apply_attempts` | `Dict[str, int]` | `operator.or_` | `{}` |
| `applied_step_ids` | `List[int]` | `operator.add` | `[]` |
| `applied_files_log` | `List[Dict[str, Any]]` | `operator.add` | `[]` |
| `auto_accept_low_risk` | `bool` | scalar overwrite | `False` |
| `last_execution_context` | `Optional[Dict[str, Any]]` | scalar overwrite | `None` |

`pending_step_files` is load-bearing: the pre-existing `pending_patches`/`pending_contents`
channels use `operator.or_` and can never be cleared, so `apply_patch` must be told which
paths belong to THIS step — a later step re-editing an earlier step's file no longer makes
the first diff unrecoverable. `pending_apply` carries diffs, not file bodies (commit re-reads
from `pending_contents`) and is a plain dict (serialized into the checkpoint's pending
writes). `last_execution_context` (`{command, exit_code, stdout_tail, stderr_tail}`) is set
ONLY alongside a `healing_required=True` signal that originated from `_commit_command`
(which also sets `failed_node="apply_commit"` in the SAME return dict) — a consumer
(`agents/error_correction.py`) MUST gate on `failed_node == "apply_commit"` before reading it,
since — like every other field in the self-healing block — it is a scalar overwrite with no
cross-turn staleness guard on its own.

**`ActivityKind` (`api/ws_contracts.py`):** one new arm, `"subagent"` (`ref = task_id`,
`target = role`) — `brain/nodes/subagent_worker_node.py::subagent_worker` previously emitted
nothing at all on the Glass-Box Timeline channel regardless of how long its tool loop ran.
Emitted via the same `push_activity` config seam `apply_gate.py`'s `diff` marker uses, not
`narrate`'s verb-classification, since this is structured dispatch metadata rather than a
repurposed free-text node label.

**`GraphMutationPayload.new_status`** (`api/ws_contracts.py`) widened to match `WBSStep.status`
above — an un-widened `Literal` would raise inside `emit_graph_mutation`'s fire-and-forget
task and fail silently the first time a step reached `awaiting_approval`/`rejected`/
`revision_requested`.

**`PatchAppliedPayload.already_applied: List[str] = []`** (`api/ws_contracts.py`) — files the
host (`PatchActuator.ts::classifyPatchEdit`) recognized as already holding the exact intended
content (a lost WS ack after a prior apply already succeeded) rather than genuinely stale, so
a retry can never falsely abort the whole atomic write as a conflict. Always a subset of
`applied_files`. Additive/diagnostic only — no backend consumer branches on it differently
today; `core/write_pipeline.py::apply_patch_set`'s caller already treats an already-applied
file identically to a freshly-applied one (both land in `applied_files`), which is the
correct behavior.

**Why the split (replay-safety).** LangGraph replays a node from the top on resume
(DEBT-185). `apply_commit`'s first statement is `if not state.get("pending_apply"): return {}`
— pure and idempotent — and its actual `interrupt()` call (via `core/hitl.py::request_graph_approval`)
is the FIRST side-effectful action in the node, mirroring the existing
`drift_compute`/`drift_gate` and `finops_gate` precedent. Idempotency against a
double-delivered resume is enforced by checking `applied_step_ids` before anything else.

**Backward-compat.** Every new channel defaults to an empty/`None` value a pre-13.0.9
checkpoint already implicitly has (`.get(...) or <empty>` everywhere it's read). The three new
`WBSStep.status` values and `agent_notes` are additive Pydantic fields — an older persisted
plan missing them deserializes with the new field(s) absent/`None`.

## §57 — Effort Budget, Acceptance-Check Execution, and Routing-Matrix Consumption (Manifest 13.1.3)

`docs/OUTPUT_BUDGET_BRIEF.md`'s investigation found a corrective-work family: a marginal
context-window failure, a completion-integrity gap (a syntactically invalid file shipped as a
reported success), and three independent instances of a computed decision that was persisted,
rendered, and never consumed. This section covers every contract touched closing them.

**`AIlienantGraphState.execution_mode` → `effort_level` (`brain/state.py`) — a rename, not an
additive widening; stated honestly rather than mischaracterized.** The old channel was a
topology selector (`Literal["SEQUENTIAL", "MICRO_SWARM", "FULL_SWARM"]`) choosing between three
separate LangGraph apps (`brain/swarms.py`, `brain/fast_path.py`, dispatched from
`brain/intent_router.py::process_user_intent`) — confirmed to have **no production caller**
(`core/task_service.py` invokes the main `alienant_app` graph directly); the three modules and
their topology-only tests are deleted (`DEBT-208`, decision record). The new
`effort_level: Literal["light", "balanced", "deep"]` channel is the Effort Budget — a
verification-DEPTH axis on the one graph every turn already ran, never a which-agents-run axis.
**Checkpoint tolerance:** `core/task_service.py::rehydrate_paused_interrupt` reads
`snapshot.values.get("effort_level") or "balanced"` — a checkpoint predating this rename (which
carries the old `execution_mode` key, if anything) deserializes with `effort_level` absent and
falls back to the default rather than raising, per the context-tolerant reading charter §10
requires of every consumer. `TaskPayload.execution_mode` (the UNRELATED permission axis:
AUTO/ASK/PLAN, `api/task_router.py`) is untouched — same name, different field, on a different
model; this rename only ever touched the graph-state channel.

**New `AIlienantGraphState` channel, additive:**

| Channel | Type | Reducer | Safe default |
|---|---|---|---|
| `check_results` | `List[Dict[str, Any]]` | `operator.add` | `[]` |

Populated by the new `run_checks` node (`brain/checks_gate.py`), a terminal graph node reached
from `route_after_validation` (`brain/guardrails.py`) only when every WBS step has reached a
terminal status AND `effort_level == "deep"` — the plan's own `MissionSpecification.checks` are
executed via the existing guarded-command path (`tools/execution_tools.py::run_guarded_command`)
for the mechanically-executable subset (pytest/mypy/ruff/npm-shaped commands); a non-command
check is excluded from `check_results`, not silently marked passed (`DEBT-205`). A failing
result folds into `errors` and blocks a "success" report — closing the gap where a plan's own
acceptance criteria could name the exact bug that shipped anyway.

**`ContextMeter.routing_decision` pattern (`brain/state.py`) widened:**
`"^(LOCAL_SMALL|LOCAL_BIG|CLOUD)$"` → `"^(LOCAL_SMALL|LOCAL_MEDIUM|LOCAL_BIG|CLOUD)$"`. A new
`LOCAL_MEDIUM` band sits between `LOCAL_SMALL` and `LOCAL_BIG` in
`core/memory/context_auditor.py::derive_routing_decision`'s TCI thresholds (split point chosen
to preserve the pinned `(50.0, 80.0, "LOCAL_BIG")` regression test). Additive to the pattern; a
checkpoint from before this change never carries `LOCAL_MEDIUM` as a value, so it still matches
the old pattern and needs no migration.

**Routing decision now actually consumed.** `core/memory/context_auditor.py::derive_routing_decision`
computed a correct decision, persisted it to `routing_decision`, and rendered it on the Context
Meter widget — but no agent read it: the planner/coder/researcher requested a hardcoded model
tier regardless. New `resolve_model_alias_for_routing(routing_decision, default)` maps
`LOCAL_SMALL`/`LOCAL_MEDIUM`/`LOCAL_BIG`/`CLOUD` → `MODEL_SMALL`/`MODEL_MEDIUM`/`MODEL_BIG`/
`MODEL_CLOUD`; planner and coder now resolve their tier through it, falling back to their prior
hardcoded constant only when no decision is present in state (a cache-hit turn, a benchmark
stub) — never a behavior change on a turn that never computed a routing decision to begin with.
`brain/routing_engine.py::select_best_agent`/`get_optimal_provider`/`resolve_provider` (a second,
divergent implementation of the same CSS × TCI matrix, confirmed zero production callers) are
deleted; `derive_routing_decision` is the one implementation. `get_keep_alive` (routing_engine.py's
only live export) is untouched.


---

## Task Complexity Index — how the score is built (13.1.6)

`routing_decision` is only as good as the TCI feeding it, and `task_complexity_index` was never
computed anywhere in production: it was seeded `0.0` and only ever overwritten by the Mini-Judge's
own verdict (`MEDIUM → 75.0`, `HIGH → 100.0`). The whole CSS × TCI matrix therefore collapsed into
a 3-state function of one classifier, and since that classifier fails toward "no risk", the common
path always scored `0.0` and routed `LOCAL_SMALL` — including a from-scratch, multi-domain project
build, which is what produced a stepless plan from a 3-billion-parameter model in a live session.

`core/memory/context_auditor.py::compute_task_complexity_index` is a pure, bounded, LLM-free score
on the same 0-100 scale, deliberately shaped like the sibling CSS formula in `agents/researcher.py`:

| Sub-signal | Weight | Observable | Saturation |
|---|---|---|---|
| breadth | 0.45 | `retrieved_files + explicit_mentions + dirty_buffers` | 8 files |
| instruction load | 0.35 | distinct action verbs + conjunction markers (0.7), prompt length (0.3) | 6 requirements / 120 words |
| greenfield premium | derived | empty corpus **and** an action verb present | binary |

**Why these saturation points.** A request already touching ~8 distinct files is broad enough that
no larger number should route differently — past that the model's problem is context capacity, which
CSS measures, not complexity. ~6 separate requirements is past the point where a small model reliably
holds the whole contract in a single structured draft. Both are anchored to observable shape, not
tuned until one example produced a desired verdict.

**The greenfield premium exists because the previous behaviour was inverted.** An empty corpus makes
retrieval return nothing, which read as "small task" — so *creating an entire project from scratch*,
the single most demanding thing a user can ask, routed to the cheapest model. `derive_routing_decision`
already skips the red-alert CSS floor when `corpus_empty` (a cold workspace is not a coverage gap), so
without this term nothing else would have raised the score either.

**Weights are derived, not restated.** The last weight in each group is computed from the others
(`_TCI_W_GREENFIELD = 1.0 - _TCI_W_BREADTH - _TCI_W_LOAD`) so the set cannot stop summing to 1.0 and
silently rescale the score off the range the bands are calibrated against (CLAUDE.md §5.7).

**Band boundaries are out of scope for this score.** `derive_routing_decision`'s 30/50/75 cut-offs are
the committed contract; TCI places a turn *within* them and must never redefine them. The Mini-Judge
verdict remains authoritative *above* the structural score, applied as a floor by the researcher's
cascade (`MEDIUM → max(tci, 75.0)`, `HIGH → 100.0`) — one implementation of the escalation rule, not
two. Consequently the score is an ordering signal, not a calibrated metric; making it continuous is
precisely what lets the Phase 8.3 harness stratify by TCI bucket (`[0,40)/[40,75)/[75,100]`) for the
first time, since all three buckets previously collapsed onto `0/75/100`.

**Untrusted input.** `user_input` is hostile (CLAUDE.md §6.2) and this runs on every turn, so the scan
is capped at `_TCI_SCAN_CAP` characters and uses only literal character classes and `str.split()` —
no nested quantifiers that could backtrack.

**`shared.hardware.HardwareProfile.suggested_mode` — removed, not additive; stated honestly.**
This field was the hardware-lock half of the same dead topology selector (a VRAM-threshold
recommendation among SEQUENTIAL/MICRO_SWARM/FULL_SWARM), surfaced only by
`HardwarePanel.tsx`'s Execution Mode card. `HardwareProfile` is a live, computed-per-request API
response (`GET /api/v1/hardware/profile`), never persisted or checkpointed, with exactly one
consumer — updated in the same change. Charter §10's additive-only rule targets contracts that
must tolerate independently-versioned readers/writers across time (persisted state, WS envelopes,
MCP schemas); a live snapshot with a single first-party consumer patched in lockstep is not that
case, so removal here is a normal API edit, not a rule exception. `VRAM_MICRO_SWARM_GB`/
`VRAM_FULL_SWARM_GB` (`shared/config.py`) are removed with it — `VRAM_CLOUD_FLOOR_GB` and
`effective_vram_gb()` (the live LOCAL-to-CLOUD reroute gate, `core/memory/context_auditor.py`)
are untouched.

**`GET`/`POST /api/v1/hardware/mode` — response shape changed, not additive; stated honestly.**
This is a live preference endpoint, never persisted: `mode`'s domain changed from
`"AUTO" | "SEQUENTIAL" | "MICRO_SWARM" | "FULL_SWARM"` to `"light" | "balanced" | "deep"`, and a
new `cost_estimates` field was added (`Dict[EffortLevel, {extra_calls, seconds_per_extra_call,
calibrated}]`, from `tools/llm_gateway.py::estimate_effort_costs`) replacing the old
hardware-VRAM lock with a stated time cost — no Effort Budget level is ever hardware-locked, so
none needed a reason to reject a selection. Both `HardwarePanel.tsx` (dashboard) and
`ModelsMenu.tsx`'s orchestration view (workspace webview, via a new `GET_EFFORT_MODE`/
`SET_EFFORT_MODE` host-bridge round trip) were updated in the same change.

---

## Design Decision — Phoenix Distributed Tracing Dependency Footprint (GO — client/server split)

> Not a wire contract (no state channel, WS event, REST field, or table in AILIENANT's own DBs —
> Phoenix owns its own store), so this record carries no `§N` number, matching the precedent set by
> the manifest 13.1.4 telemetry additions, which also got none. Answers CLAUDE.md §9's demand for an
> explicit dependency-governance justification before `core/observability.py::configure_phoenix_tracing`
> shipped, and records the real footprint after two initial assumptions were checked against the
> installed packages and corrected.

**Decision: GO, split into a light client (Python) and a heavy server (Docker), not one `pip install`.**
The manifest's original sketch named `arize-phoenix` — the whole platform (~40-60 transitive
packages: strawberry-graphql, pandas, uvicorn, sqlean…) — against the standing §9 precedent
(`scipy` rejected for a narrow need in favor of a hand-rolled `degree_centrality`). Arize ships the
server as a separate `arizephoenix/phoenix:latest` image and a lightweight client package
(`arize-phoenix-otel`), so only the client enters `requirements-dev.txt`; the server runs as a
`profiles: ["tracing"]` compose service, never started by a bare `docker compose up`.

**Honest footprint (verified by an actual install, not a metadata read):** `arize-phoenix-otel`
depends on the `opentelemetry-exporter-otlp` *meta-package*, which pulls **both** the gRPC and HTTP
OTLP exporters — meaning `grpcio` (a ~5MB compiled wheel) installs regardless of which protocol is
actually used at runtime (`http/protobuf`, chosen here — see `core/observability.py`). An earlier
draft of this decision assumed choosing HTTP would avoid `grpcio`; it does not. The split still
avoids the platform package's much larger surface (strawberry-graphql, pandas, uvicorn, sqlean), so
the decision stands, but on the real number: 15 packages installed
(`arize-phoenix-otel==0.17.1`, `openinference-instrumentation-langchain==0.1.72`,
`openinference-instrumentation-litellm==0.1.39`, and their transitive OTel/protobuf/grpcio tree),
all dev-only per the DEBT-179 split (never in the runtime image).

**Scope: both LangChain and litellm instrumentors, not just the graph.** No agent in this codebase
uses a LangChain chat model — every LLM call is a raw `litellm.acompletion()`
(`tools/llm_gateway.py`, 11 call sites). `LangChainInstrumentor` alone would produce graph-node spans
with empty interiors — timings with no prompt, completion, token, or model detail, the exact
information a debugging session usually needs first. `openinference-instrumentation-litellm` closes
that gap. Verified directly against the installed package source (not assumed from its own docs,
which warn instrumentation "must run before `import litellm`" — a warning describing a narrower
failure mode, an aliased `from litellm import acompletion` import, which does not occur anywhere in
this repo): `LiteLLMInstrumentor._instrument()` patches via plain `setattr(litellm, "acompletion", …)`,
an attribute resolved at call time. Every one of the 11 call sites uses `litellm.acompletion(...)`
module-attribute access, so late patching from `main.py`'s `lifespan()` — long after
`tools/llm_gateway.py`'s own `import litellm` — still wraps the same callable it calls. Proven by a
live test (`tests/test_observability.py::test_litellm_acompletion_is_wrapped_and_produces_a_span`),
not assumed.

**Cost when off is structurally zero**, not merely small: `configure_phoenix_tracing()` checks the
env flag and returns before importing anything from the tracing tree. **Cost when on** was not yet
measured against a real workload at the time this was recorded — a traced-vs-untraced turn
comparison is a follow-up measurement, not a blocker to shipping the opt-in path itself.

**Known trade-off, stated plainly rather than left implicit:** when tracing is on, prompts and
completions are written into Phoenix's local SQLite store, outside `SecretsScrubberFilter`'s reach —
the one sink in this project that does not route through that filter. Local-only and opt-in, but a
real departure from the project's usual "secrets never round-trip through a sink" posture
(`DEVELOPERS.md`'s Testing & quality gates section states this to anyone about to turn it on).

---

## §58 — Reviewable Distillation Brief: `pending_brief` / `brief_revision_note` (Manifest 13.1.8)

**Additive.** Two new channels on `AIlienantGraphState` and one new `request_kind` value. No
existing field changes shape, and no consumer that ignores them behaves differently.

### Channels

```python
pending_brief: Optional[Dict[str, Any]]     # {"composed": str, "glossary": Dict[str, str]}
brief_revision_note: Optional[str]
```

Both are scalar-overwrite (last-value) channels, cleared once the review resolves.

`pending_brief` exists for the same reason `pending_grill_batch` does (§53), and the cost of
omitting it is higher. `run_synthesis_node` now spans two graph super-steps driven by a
`synthesis_node` self-loop:

- **Draft phase** (`pending_brief` empty): distils, composes, commits to `pending_brief`,
  returns — no `interrupt()` in this invocation.
- **Review phase** (`pending_brief` set): suspends on the STATE-SOURCED brief as the node's
  first action.

LangGraph replays a node from the top on every resume. Drafting and interrupting in one
invocation would therefore re-run the `MODEL_BIG` distillation on every review round — charging
for it again and non-deterministically replacing the exact text the operator was looking at with
one they never saw. `pending_grill_batch` guards the same hazard for a cheaper call.

`brief_revision_note` carries the operator's correction into the next draft. It is appended to
the distillation's **user payload**, never to `_DISTILL_SYSTEM_PROMPT`, so the system message
stays byte-identical across drafts.

### `request_kind = "BRIEF_REVIEW"`

`HITLApprovalRequestPayload.request_kind` is an open `Optional[str]`, not a closed `Literal`, so a
new value is additive by construction. The review reuses `core/hitl.py::request_graph_approval`
unchanged — it already returns `{"approved", "comment", "modified_content"}`, which is exactly the
accept / rewrite-with-note / cancel shape this needs — so there is no new wire message, no new
resume path, and no change to `useHitlResponder`.

A client that does not recognise the kind falls through to the generic intervention card and can
still answer, because the payload is a plain approval.

### Why the brief is reviewed at all

The distillation replaces `user_input` and is the only stage in the pipeline nothing checks: the
coder has `validate_output` plus acceptance checks, the planner has its Actor-Critic loop, the
grill has a human on every round, and the Actor-Critic validates the resulting plan against a
*schema* rather than against the dialogue. The transcript itself survives in `messages` (an
accumulating channel) but no downstream node reads it, so an omission introduced here is neither
detected nor recoverable — and it surfaces as an absence, which no view can render.

### Rendering constraint

The review surface renders the composed brief **verbatim and uncapped**. `_compose_planner_brief`
already emits the settled constraints and scope as labelled blocks, so the structure the reviewer
needs is in the text itself — deriving separate UI sections would risk displaying something the
planner does not receive, and truncating (as the generic card does at 800 chars) would hide part
of what is under review.

## §59 — Glass-Box Timeline Agent Attribution: `role` / `model_tier`, `ActivityKind.tool` (Manifest 13.1.9)

**Additive.** Two new optional fields on `ActivityEventPayload` and one new `ActivityKind` member.
No existing field changes shape, and an older client that ignores both renders exactly as before.

### Fields

```python
class ActivityEventPayload(BaseModel):
    ...
    role: Optional[str] = None        # the acting agent, e.g. "researcher", "coder", "core_dev"
    model_tier: Optional[str] = None  # "small" | "medium" | "big" | "cloud"
```

Sourced from two contextvars on `core/activity_context.py` (`current_agent_role`,
`current_model_tier`), read by `core/task_service.py::_push_activity` when a caller does not pass
either explicitly — which is every existing call site, so no narration/diff/reasoning call needed
to change. Two binders, two precedences, mirroring `bind_activity_sink`'s existing reset-in-finally
contract (§5.1):

- **Outer default** — `brain/engine.py::_instrument_node` (the parent graph) and
  `brain/ideation.py::_guarded` (the ideation subgraph) derive a role from the node's own
  registered name (`brain/state.py::derive_node_role` — strip a `_agent`/`_node` suffix; a one-entry
  alias table covers "analyst_grill" -> "analyst", the sole case where the node's name and its
  dispatcher's RBAC identity were never the same string) and bind it for the whole node call,
  resetting in `finally` regardless of how the node exits.
- **Narrower override** — `core/tool_dispatch.py::ToolDispatcher.dispatch` binds
  `self._activity_role` for the duration of one tool call, then resets back to the node's own
  default. `ToolDispatcher.__init__` gained an optional `activity_role` parameter, defaulting to
  `active_role` (RBAC identity) when omitted — the two are the same string for researcher/analyst/
  a dispatched subagent, but the coder's `active_role` is the per-WBS-step `target_role` ("core_dev",
  "qa_tester", …), which is real and meaningful for permission gating but would fragment one coding
  turn into a new lane every time the step's role changes. `agents/coder.py` passes
  `activity_role="coder"` to keep the lane stable while leaving RBAC untouched.

Model tier is bound once per node, immediately after the three
`resolve_model_alias_for_routing` call sites resolve it (`agents/planner.py` ×2, `agents/coder.py`)
and at the two pinned constants (`agents/analyst.py::_GRILL_TIER`,
`brain/ideation.py::_SYNTHESIS_TIER` — both derived from their model alias against the resolver's
own tier vocabulary, mirroring `_GRILL_TIER`'s existing pattern, never a second hardcoded ladder).
Bind-and-forget: the owning node wrapper's `finally` is the single guaranteed reset point, so an
internal call site does not need its own token/reset pair.

### `ActivityKind.tool`

`ToolDispatcher.dispatch` already instrumented every tool call (DEBT-133, 12.8) but hardcoded
`kind="command"` on the marker it emitted — a registry/MCP tool call (`grep_index`, `read_file`)
rendered identically to a real shell command, and the role that WAS captured
(`initiator=self._active_role` on the I/O detail) never reached the row itself. `ActivitySink.
emit_marker`/`emit_blocked` gained an optional `kind: str = "command"` parameter (default preserves
`core/exec_log.py::record_execution`'s existing shell-command behavior unchanged); `ToolDispatcher`
passes `kind="tool"` on both its ref-carrying marker and its blocked-attempt marker, and
`brain/agentic_cell.py`'s pending-tool-call HITL-defer path (which opens the span before its own
`interrupt()`) does the same.

### Frontend

`TimelineEntry` gained `role?: string` / `modelTier?: string`, populated by
`timelineBuilder.ts::upsertActivityMarker` from the new payload fields. `utils/agentLanes.ts` (new,
pure) folds consecutive same-role entries into `AgentLane[]`, **replacing** the 13.0.7 work-loop
phase grouping (`activityLabels.ts`'s `timelineEntryPhase`/`workLoopPhaseLabel`, deleted): the agent
already implies the phase in practice, and a second grouping axis over a handful of rows made a
short turn look broken rather than short. Each lane header renders the agent's display name
(`formatRoleLabel`) and, when present, the tier + real model name (`formatModelBadge`, joined
against `AilienantConfig.tiers` — already a global field on `chatStore`, no new round-trip needed).

## §60 — Reviewable Model Route: `confirmed_routing_decision` (Manifest 13.1.10)

**Additive.** One new channel on `AIlienantGraphState` and one new `request_kind` value. No
existing field changes shape, and a client that does not recognise the kind falls through to the
generic HITL card and can still answer, since the payload is a plain approval.

### Channel

```python
confirmed_routing_decision: Optional[str]  # "LOCAL_SMALL" | "LOCAL_MEDIUM" | "LOCAL_BIG" | "CLOUD"
```

Scalar overwrite, `None` before the gate runs or after a cancelled review. Read by all three
`resolve_model_alias_for_routing` call sites (`agents/planner.py` ×2, `agents/coder.py`) in place
of the raw `context_metrics.routing_decision`, falling back to that raw value when
`confirmed_routing_decision` is unset (a direct node-level test, or any turn that never passed
through `model_route_gate`). This is what makes one confirmed choice cover every WBS step of a
turn — the coder reads a decision already made, never re-gating per step.

### `brain/routing_gate.py::run_model_route_node`

Spliced between `researcher_agent` and `planner_agent` in both graph topologies
(`ENABLE_DYNAMIC_DISPATCH` on or off) — every path to the planner already runs through the
researcher first (`route_after_ideation` hands off through `researcher_agent`, not directly to
`planner_agent`), so this is the single choke point that sees a computed
`context_metrics.routing_decision` no matter which route got here. The dynamic-dispatch topology's
conditional-edge path-map is remapped (`"planner_agent": "model_route_gate"`) rather than the
router function itself touched, the same technique `brain/engine.py` already uses to splice
`researcher_agent` in front of every planner entry.

**Deliberately single-phase**, unlike `run_synthesis_node`'s defer-then-interrupt-first split
(§58): that split exists because drafting a brief is an expensive, non-deterministic `MODEL_BIG`
call that must not silently re-run on every resume. Resolving a routing decision here is a pure
re-read of `context_metrics`, already sitting in state — replaying the node from the top on resume
reproduces the exact same value every time, so resolving and interrupting within one invocation
carries no hazard. `tests/test_routing_gate.py` pins the review function to at most one call per
invocation as the test that would catch a regression of this assumption.

**Mode-aware.** Only `SessionPermissionMode.CAUTIOUS` and `.PLAN_ONLY` — the two modes the 3-button
frontend UI's "Ask" and "Plan" buttons actually produce (`core/permissions.py`'s
`_FRONTEND_MODE_TO_SESSION`) — suspend for a decision. Every other session mode, "Auto" (`STANDARD`)
foremost, bypasses the gate and writes the router's own pick immediately: the Glass-Box Timeline's
lane badge (§59) remains the only surface for the model choice in that mode, consistent with what
Auto is for. Scoped deliberately narrow — a future session mode reachable from the UI needs this
predicate revisited, not silently assumed to gate correctly.

### `request_kind = "MODEL_ROUTE_REVIEW"`

Reuses `core/hitl.py::request_graph_approval` unchanged, exactly as §58's brief review does — no
new wire message, no new resume path, no change to `useHitlResponder`. The drafted route (decision
+ TCI + CSS, the justification the operator needs to judge the choice rather than rubber-stamp it)
travels as a JSON string inside the existing `proposed_content` field
(`ailienant-extension/src/workspace/utils/modelRouteLogic.ts::parseRoutePayload`, defensive —
malformed JSON degrades to a minimal accept/cancel render, never crashes the card) rather than as a
new field, since `request_graph_approval`'s payload has only one `str` content slot. The three
decisions all use the SAME `approved`/`modified_content` shape §58 already established: accept
(`approved=true`, no override), pick a different tier (`approved=true`,
`modified_content=<routing_decision string>`), cancel (`approved=false`, ends the turn — `hitl_pending`
set, `confirmed_routing_decision` cleared rather than left stale).
