# SCHEMA_EVOLUTION.md
## LangGraph State Definition para Ialienant

Este documento define el esquema estricto de datos que fluye a través del grafo de LangGraph. Se implementará en Python utilizando `typing.TypedDict` y validación con `pydantic`.

### 🧠 1. Core State Schema (`AIlienantGraphState`)

```python
from typing import TypedDict, List, Dict, Any, Optional, Annotated, Literal
from pydantic import BaseModel, Field
import operator

class ContextMeter(BaseModel):
    semantic_similarity: float  # 0.0 a 1.0
    graph_coverage: float       # 0.0 a 1.0
    recency_score: float        # 0.0 a 1.0
    css_total: float            # Context Sufficiency Score (0 - 100%)
    task_complexity_index: float # TCI (0 - 100%)
    routing_decision: str       # "LOCAL_SMALL", "LOCAL_BIG", "CLOUD"
    is_red_alert: bool          # True si CSS < 40%

class LLMProfile(BaseModel):
    model_name: str            # ej. "llama3:8b-instruct-q4_K_M"
    parameters_b: float        # ej. 8.0, 1.5, 32.0 (Para calcular capacidad lógica)
    context_window: int        # ej. 8192 (Para evitar crashes de ventana)
    quantization: str          # ej. "int4", "fp16"

class TokenCounter(BaseModel):
    local: int = 0
    cloud: int = 0
    total_cost_usd: float = 0.0

# --- Virtual File System (VFS) ---
class VFSFile(BaseModel):
    content: str = Field(..., description="Contenido en texto plano del archivo")
    document_version_id: str = Field(..., description="Timestamp o Hash para OCC")
    is_dirty: bool = Field(default=False, description="True si requiere sincronización con el IDE")

class EnvironmentProfile(BaseModel):
    os_name: str
    vram_available_gb: float
    # Registro de capacidades. El orquestador lee esto antes de enrutar.
    available_models: Dict[str, LLMProfile] # Map de roles ("LOCAL_SMALL", "CLOUD") a sus perfiles reales

# --- Spec-Driven Development (WBS) ---
class WBSStep(BaseModel):
    step_number: int
    target_role: Literal["Refactor", "Infra", "Doc", "SecOps", "Test"] = Field(
        description="Rol que asumirá el CoderAgent para esta tarea."
    )
    action: Literal["read_file", "write_file", "edit_file", "run_command"]
    target_file: str
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"

class MissionSpecification(BaseModel):
    """El Macro-Contrato que blinda la ejecución del CoderAgent."""
    outcome: str
    scope: List[str]
    constraints: List[str]
    decisions: List[str]
    tasks: List[WBSStep]
    checks: List[str]

# --- INSERCIÓN: Control de Estado HITL y Planner Manual ---
PlanningStrategy = Literal["autonomous", "manual_override", "collaborative"]

class AIlienantGraphState(TypedDict):
    # --- Memoria Base y Petición ---
    task_id: str
    user_input: str
    messages: Annotated[List[Dict[str, str]], operator.add]
    
    # --- Telemetría Dinámica ---
    context_metrics: ContextMeter
    active_llm_profile: LLMProfile
    token_usage: TokenCounter
    
    # --- Control de Flujo (Prompt Swapping) ---
    is_manual_override: bool 
    target_role: Optional[str]        # El rol forzado actual si hay override
    current_step_id: Optional[int]    # ID de la tarea actual en ejecución
    
    # --- Planificación Inmutable (SDD) ---
    mission_spec: Optional[MissionSpecification] 
    
    # --- VFS (Single Source of Truth) ---
    read_files_state: Dict[str, VFSFile]
    vfs_buffer: Dict[str, VFSFile]
    
    # --- Diagnóstico ---
    errors: Annotated[List[str], operator.add]
    retry_count: int
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str

    # Control de Concurrencia y Planificación
    planning_strategy: PlanningStrategy
    human_wbs_draft: Optional[str] # Payload crudo del IDE. Solo primitivas, NO objetos de conexión.
    wbs_approved: bool # Bandera de transición. Si es False, el grafo interrumpe (HITL).

```

---

### 📋 2. Reglas de Transición (Mutaciones de Estado)

Para garantizar un flujo predecible, evitar colisiones en LangGraph y operar bajo la nueva arquitectura de **Prompt Swapping**, los permisos de mutación y uso de herramientas sobre el `AIlienantGraphState` están estrictamente centralizados:

* **🕵️ ResearcherAgent (Retrieval Only):** Operación estricta de **Solo Lectura** sobre el sistema de archivos. Único agente autorizado para mutar el contexto inyectando el `skeleton_prompt`. No tiene permisos para alterar código fuente.
* **📐 PlannerAgent:** Único agente autorizado para inicializar y mutar el `wbs_plan`. Durante su ejecución ($O(1)$), el grafo entra en "modo plan", bloqueando globalmente cualquier herramienta de escritura (`Write Tools`).
* **🧠 OrchestratorAgent:** Único agente con autoridad para gestionar el ciclo de ejecución ($O(N)$). Muta las métricas de ruteo inyectando la **Matriz de Enrutamiento 3D** (calculando el $CSS$). 
  * *Drift Detection:* Tiene autoridad absoluta para bloquear el grafo e inyectar el estado `HITL_APPROVAL_REQUIRED` si detecta que la ejecución intenta desviarse del `immutable_wbs`. No posee permisos de escritura de código.
* **⚙️ CoderAgent (El Obrero Mutante):** Es el **ÚNICO nodo** con permisos de escritura (`Write`) y ejecución (`Execute`). Sus permisos de mutación cambian dinámicamente en tiempo real según el System Prompt (Rol) inyectado por el Orchestrator:
  * *Rol Refactor:* Permisos restringidos exclusivamente a `BatchEditTool` para mutaciones quirúrgicas. Tiene prohibido reescribir el archivo completo.
  * *Rol Infra:* Permisos sobre Docker y Bash. Si intenta mutar un archivo `.env` o usar la `BashTool` para despliegues, el estado transita forzosamente a una interrupción (Human-in-the-Loop).
  * *Rol Doc:* Permisos de escritura limitados a la generación de JSDoc/Docstrings y mutación de archivos `.md`.
  * *Rol SecOps:* Muta el código basándose estrictamente en el output del `RunLinterTool` y reglas OWASP. 
  * *Rol Debug & Test:* Opera en un Micro-Enjambre cerrado. Utiliza la `BashTool` para ejecutar pruebas. **Regla de Bloqueo:** La tarea no puede transitar a "completada" en el estado hasta que reciba un *exit code 0*. Utiliza `FileEditTool` para inyectar los parches basados en el `stderr`.
* **🐜 AnalystAgent (La Hormiga):** Operación inmutable (**Solo Lectura**). Muta el estado conversacional de la UI, pero nunca el código. Utiliza herramientas `ReadOnly` (como `RunLinter` o `FileReadTool`) cruzando la memoria a corto plazo (`IalienantGraphState`), la memoria a largo plazo (GraphRAG) y los `dirty_buffers` del IDE para generar críticas usando el Método Socrático.

### 💾 3. Persistencia y Consolidación de Estado (AgentMemory)
 es el motor de infraestructura subyacente (Checkpointer).

* **Regla de Consolidación (Commit):** El estado temporal del grafo (`AIlienantGraphState`) no persiste automáticamente. Toda mutación de código confirmada y ejecutada por el `CoderAgent` debe pasar por la validación de hashing del motor de memoria.
* **Sincronización:** Solo tras obtener un exit code exitoso (ej. del micro-enjambre de Testing) y pasar el hashing, el código se refleja permanentemente en la memoria a largo plazo (LanceDB / NetworkX), actualizando el contexto para futuras recuperaciones del `ResearcherAgent`.

### 🛡️ 4. Phase 2.26 — Contract Guard Fields (Additive Schema Growth)

`AIlienantGraphState` gana dos campos opcionales. `ContextMeter` Pydantic permanece **inmutable**.

| Campo | Tipo | Owner (mutación) | Semántica |
|---|---|---|---|
| `ui_payload` | `Optional[Dict[str, object]]` | `ContractGuardNode` | Señal de render para la extensión VS Code. Cuando llega no-None con `action="RENDER_PERSISTENT_CONTRACT"`, el frontend dibuja el banner persistente y luego lo *ack* (vuelve a `None`). |
| `contract_anchor` | `Optional[Dict[str, object]]` | `ContractGuardNode` (sólo) | Snapshot de `{tci, target_role, turn}` en la última emisión. Read-only para el resto del grafo. `None` hasta la primera emisión. |

Ambos campos son *scalar overwrite* (sin reducer) — no se aplican en fan-out paralelo porque `ContractGuardNode` corre en serie tras `coder_agent`.

**Invariantes deterministas (O(1)):**

* **TCI Delta:** `abs(state["tci"] - anchor["tci"]) > 15.0` (puntos absolutos sobre 0–100). Requiere anchor previo.
* **CSS at Token Capacity:** `state["css"] < 40.0` **AND** `(token_usage.local + token_usage.cloud) / active_llm_profile.context_window >= 0.80`. Es la única regla que puede disparar en el primer turno (sin anchor).
* **Subgraph/Domain Shift:** `state["target_role"] != anchor["target_role"]` (y `target_role is not None`). Requiere anchor previo.

**Contrato del payload:**

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

`SessionContract` (Pydantic) es el contrato vinculante para el LLM y la extensión. Cualquier fallo de red, parse o validación del LLM cae a un esqueleto determinista construido desde `mission_spec.outcome / .scope / .constraints` — el banner siempre se renderiza si el trigger se activó.

### 🚦 5. Phase 2.27 — Resource Broker Fields (Additive Schema Growth)

Tres campos opcionales nuevos en `AIlienantGraphState`. `ContextMeter` Pydantic permanece **inmutable**.

| Campo | Tipo | Owner (mutación) | Semántica |
|---|---|---|---|
| `ui_interrupt` | `Optional[Dict[str, object]]` | `ResourceBroker` | Señal de modal bloqueante. **Distinto** de `ui_payload` (banner persistente). Cleared por la extensión al elegir resolución. |
| `contention_status` | `Optional[Dict[str, object]]` | `ResourceBroker` (sólo) | Telemetría del momento de contención: `requested_model`, `holder_session`, `queue_len`, `recommendation`. |
| `user_resource_resolution` | `Optional[Literal["WAIT","SWITCH_TO_CLOUD","CANCEL"]]` | `ResourceBroker` (lee del HitL reply) | Respuesta del usuario consumida dentro del mismo nodo. |

Los tres son *scalar overwrite* (sin reducer) — el broker corre serialmente desde los sitios de llamada protegidos.

**Heurística de recomendación (`_compute_recommendation`):**

* `TCI > 75` → `SWITCH_TO_CLOUD` (tarea pesada, cloud es más rápido)
* `TCI < 40` → `SWITCH_TO_CLOUD` (tarea ligera, no hacer esperar al usuario)
* `40 ≤ TCI ≤ 75` + queue vacío → `WAIT`
* `40 ≤ TCI ≤ 75` + queue ocupado → `SWITCH_TO_CLOUD`

**Contrato `ui_interrupt`:**

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

**Transporte WebSocket (sin cambios en `ws_contracts.py`):**

El broker reutiliza `vfs_manager.request_human_approval(...)` (mismo patrón que `drift_monitor` y `finops_gate`). Distinción vía sentinel:

* `action_description == "RESOURCE_CONTENTION"` → la extensión deserializa `proposed_content` como JSON para obtener el payload rico.
* Respuesta del usuario en `client_hitl_response` con `comment ∈ {"WAIT","SWITCH_TO_CLOUD","CANCEL"}`. El campo `approved` se ignora para esta acción.

**Disciplina anti-deadlock (CRÍTICO):**

Cada sitio de llamada (planner, summarizer, mcts_coder) DEBE envolver toda la región lock-held — no sólo la llamada `LLMGateway.ainvoke`, sino también el parsing y la validación Pydantic — en `try/finally` que invoque `await ResourceBroker.release(session_id)` si `decision.holds_lock`. Un error de parsing JSON que escape sin liberar el lock dejaría bloqueadas todas las demás sesiones permanentemente. La regression está cubierta por `test_lock_released_when_post_llm_processing_raises`.

---

## Phase 5.1 — Permission Engine + Cognitive Quarantine channels (2026-05-17)

**Strictly additive. No removals, no renames, no Literal narrowing.** Existing checkpoints deserialize unchanged because every new field has a safe default at construction sites.

**New enums in `core/permissions.py`:**

* `SessionPermissionMode` — `{DEFAULT, PLAN, AUTO}` — per-mission HITL policy.
* `ToolPrivilegeTier` — `{READ_ONLY, WRITE, EXECUTE, DANGEROUS}` — per-tool tier declared at registration.
* `PermissionDecision` — `{ALLOW, HITL, DENY}` — outcome of the pure `evaluate_action()` matrix.
* `PermissionDeniedError(Exception)` — raised by `rbwe_guard`, surfaced to agent scratchpad (not crash).

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

## Phase 5.2 — Tool RAG + MCP transport (2026-05-18)

**Strictly additive. No state-field renames or removals; no enum widening; no breaking change to `mcp_adapter` public API.**

**New constants in `core/tool_rag.py`:**

* `TOOL_RAG_TOP_K = 5` — hard cap on schemas returned per `select_tools()` call.
* `TOOL_RAG_MIN_REDUCTION = 0.70` — mean prompt-size reduction target (asserted by Phase 5.7 gate).
* `MCP_HANDSHAKE_TIMEOUT_SEC = 5.0` — `asyncio.wait_for` deadline for `bootstrap_mcp_session()`.

**New classes/functions:**

* `ToolSchema` (frozen dataclass) — in-memory representation `{name, description, json_schema, privilege_tier, allowed_roles (frozenset[str]), embedding (List[float] | None)}`. On disk in LanceDB, `allowed_roles` is persisted as a comma-flanked string for future `.where()` filtering.
* `ToolRAGStore` — RAM-resident (tempfile-backed) LanceDB store, schemas-only. Methods: `register_schema()`, `select_tools()`, `all_schemas()`, `clear()`, `prompt_size_metrics()`.
* `core/tool_rag.tool_rag_store` — module-level singleton.
* `tools/mcp_adapter.bootstrap_mcp_session(uri, state, *, timeout_sec)` — opens stdio MCP session, harvests `session.list_tools()`, populates Tool RAG store + `core.db.tool_registry` SQLite table. Returns `bool`; never raises.
* `brain/swarms.tool_rag_select_node(state)` — graph node spliced before `coder_agent` in MICRO_SWARM (also covers FULL_SWARM via embedded sub-graph).

**New env var:**

* `AILIENANT_MCP_SERVER_URI` — default `None` ⇒ local-only fallback. Format: `stdio:///absolute/path/to/server[?arg=val]`.

**`AIlienantGraphState` channels (NO new fields, only consumption changes):**

* `tool_registry_active` — now written by `tool_rag_select_node` per turn.
* `mcp_server_endpoint` — written by `bootstrap_mcp_session` at app start.
* `permission_audit_log` — appended by both above with `event="tool_rag_select"` / `"tool_rag_fallback"` / `"mcp_bootstrap_success"`.

Existing checkpoints deserialize unchanged. `mcp_adapter._call_mcp_tool` no longer raises `NotImplementedError`; it now raises `RuntimeError` if called before `bootstrap_mcp_session()` — semantic change is opt-in (callers that never bootstrap also never invoke the adapter).

**Known tech debt:** the two `operator.add` channels (`permission_audit_log`, `rbwe_violations`) grow O(N) over long missions; deferred reducer swap to truncating `(old + new)[-cap:]` in Phase 5.6/5.7. Field names/types remain stable; callers will not see the change.

---

## Phase 5.3 — Perception tools bundle (2026-05-18)

**Strictly additive. No state-field renames or removals; no `AIlienantGraphState` channel additions; no enum widening.** Five ReadOnly tools registered in `tool_rag_store` with `privilege_tier=ToolPrivilegeTier.READ_ONLY`.

**New helpers:**

* `core/db.py::get_dependents(target, project_id="")` — async, returns `List[str]` of files importing the target (1-hop backward edge from `dependency_graph`).
* `core/memory/graphrag_extractor.py::GraphRAGDynamicExtractor.bfs_k_hop_forward(seed, k)` — public alias of the existing `_bfs_k_hop` walker.
* `core/memory/graphrag_extractor.py::GraphRAGDynamicExtractor.bfs_k_hop_backward(seed, k)` — symmetric BFS using reversed edges (`target_dependency` IN frontier → `source_file`).
* `tools/agent_tools.py::make_read_file_tool(vfs_read, *, vfs_stat=None, record_read=None)` — adds `offset` / `limit` pagination + a `record_read(path, VFSFile)` audit hook. The graph-node wiring (orchestrator-side recorder writing to `state["read_files_state"]`) is a Phase 5.4 deliverable.
* `tools/perception_tools.register_perception_tools(store)` — async, registers 5 schemas (`document_parser`, `inspect_ast_node`, `get_symbol_references`, `trace_data_flow`, `web_fetch`).

**New dependencies:**

* `pypdf==5.4.0` — pure-Python PDF parser, lazy-imported by `DocumentParserTool._parse_pdf`.
* `markdownify==1.1.0` — pure-Python HTML→Markdown, lazy-imported by `WebFetchTool._fetch`. Pulls in `beautifulsoup4==4.14.3` + `soupsieve==2.8.3` (pure Python).

No semantic change to `mcp_adapter._call_mcp_tool` or Phase 5.2 contracts. Existing checkpoints deserialize unchanged.

---

## Phase 5.4 — Surgical Mutation Tools (WRITE bundle) — 2026-05-18

**Strictly additive. No state-field renames or removals. `core/permissions.py` byte-identical — only its `ToolPrivilegeTier.WRITE` enum value is imported as a schema-tier marker.** Three WRITE-tier tools registered in `tool_rag_store`.

**New tools (LangChain BaseTool subclasses in `tools/mutation_tools.py`):**

* `AtomicCodePatchTool` — wraps `tools.patch_tool.apply_patch_to_vfs` (fuzzy match + Python AST + OCC).
* `BatchSemanticEditTool` — multi-file ACID transaction via the Unit-of-Work / Write-Buffer pattern. Three phases (Phase 1 pre-validate OCC, Phase 2 apply against in-memory `Dict[str, str]` buffer, Phase 3 commit). Any failure in Phase 1 or 2 discards the buffer — the real `vfs_write` is invoked only after every item passes.
* `FileWriteTool` — full-file create/overwrite. Optional OCC via `expected_hash`. Python files are AST-validated before commit. RBWE is enforced **upstream** by `core/permissions.rbwe_guard` (Phase 5.1) — this tool's body is not state-aware.

**New helper:**

* `tools/agent_tools.py::make_state_aware_read_file_tool(state, vfs_read, *, vfs_stat=None)` — the integration seam that completes Phase 5.3's `record_read` hook. Builds a state-mutating callback that writes the `VFSFile` audit record into `state["read_files_state"]`. Agent-layer callers swap this in for `make_read_file_tool` to satisfy `rbwe_guard`.

**Role whitelist** (`_ALLOWED_MUTATION_ROLES`): `frozenset({"core_dev", "architect_refactor", "secops", "data_ml_engineer", "devops_infra"})`. Matches Phase 4 §3.2 RBAC matrix. Excludes `doc_manager` (scope-limited refinement deferred), `qa_tester` (EXECUTE tier domain), `vcs_manager` (git-only).

**No new state channels. No `core/permissions.py` edits. No new dependencies.** Existing checkpoints deserialize unchanged.

---

## Phase 5.5 / 5.6 — Async Execution + Cognitive Control bundles — 2026-05-18

**Strictly additive. No state-field renames or removals. `core/permissions.py` byte-identical — only its `ToolPrivilegeTier.{READ_ONLY, EXECUTE}` enum values are imported as schema-tier markers.** Six new schemas registered in `tool_rag_store` (4 execution + 2 control).

**New tools — `tools/execution_tools.py` (Phase 5.5, EXECUTE bundle):**

* `SandboxBashTool` — `ToolPrivilegeTier.EXECUTE`. Short-lived shell command via `asyncio.create_subprocess_shell` + `asyncio.wait_for` (30 s default timeout, kill-on-overrun). Output capped at `TASK_OUTPUT_TRUNC = 2000` chars with middle truncation. **HITL Interceptor**: every invocation pre-checks against `DANGEROUS_COMMANDS_REGEX` (imported from `tools.control_tools`); any match blocks the spawn and returns a sentinel string redirecting the agent to `ask_user_question`.
* `TaskCreateTool` — `ToolPrivilegeTier.EXECUTE`. Spawns a long-running asyncio subprocess; returns a UUID-hex `task_id`. State mutation happens inside the injected `BackgroundTaskManager` (registry is a reference to `state["background_tasks"]`).
* `TaskGetTool` — `ToolPrivilegeTier.READ_ONLY` (per blueprint §4 line 272 — the tool only reads the in-memory registry, no subprocess spawn; marking it EXECUTE would block status polling from PLAN mode).
* `CheckTypeIntegrityTool` — `ToolPrivilegeTier.EXECUTE`. `mypy --strict <dir>` or `npx --no-install tsc --noEmit -p <dir>` via `create_subprocess_exec`. Same truncation rules.
* `BackgroundTaskManager` (non-BaseTool helper class) — owns the lifecycle of long-running `asyncio.subprocess.Process` handles. Strong-ref `Set[asyncio.Task]` mirrors `agents/coder.py:101` precedent. Mutates the supplied registry dict directly.

**New tools — `tools/control_tools.py` (Phase 5.6, CONTROL classification):**

* `AskUserQuestionTool` — registered as `ToolPrivilegeTier.READ_ONLY` so the session-mode filter admits it in every mode (READ_ONLY is universally permitted, behaviourally satisfying the blueprint §4 line 277 "policy-neutral across the matrix" requirement without extending the enum). Mutates `state["pending_hitl_request"] = {request_id, kind, question, context, suggested_options, requested_at}` and returns sentinel string `[ask_user_question] HITL_PENDING:{request_id}`. Orchestrator graph node detects the populated channel and suspends the turn.
* `TogglePlanModeTool` — `ToolPrivilegeTier.READ_ONLY`. Mutates `state["session_permission_mode"]` with `Literal["DEFAULT", "PLAN", "AUTO"]` per blueprint §4 line 275. (The system prompt's `execution_mode` / topology-swap interpretation was rejected — that's an orthogonal Phase 4.3 concern.)

**New module-level export — `tools/control_tools.DANGEROUS_COMMANDS_REGEX`:** `List[re.Pattern[str]]` containing 10 patterns covering `rm -rf`, `sudo`, `drop table`, `dd if=…of=/dev/`, fork-bomb, `mkfs`, `chmod -R 777`, `> /dev/sd?`, pipe-to-shell, `git push --force`. Imported by `SandboxBashTool` for HITL interception. Will eventually drive the Frontend friction modal (Phase 5.7).

**New factories — `tools/agent_tools.py`:**

* `make_task_create_tool(state)` — captures `state.setdefault("background_tasks", {})` by reference, constructs a `BackgroundTaskManager`, returns a `TaskCreateTool` bound to it.
* `make_task_get_tool(state)` — symmetric to the above. Independent manager instance, same backing dict (factories are pair-friendly: a TaskGetTool can read entries written by a separate TaskCreateTool because both share the registry through `state`).
* `make_ask_user_question_tool(state)` — passes the state mapping to `AskUserQuestionTool(state=state)`.
* `make_toggle_plan_mode_tool(state)` — passes the state mapping to `TogglePlanModeTool(state=state)`.

**Role whitelists:**

* `tools/execution_tools._EXECUTE_ROLES`: `frozenset({"core_dev", "devops_infra", "secops", "qa_tester", "data_ml_engineer"})` — 5 hands-on roles. `architect_refactor` excluded (planning role); `doc_manager` and `vcs_manager` excluded by scope.
* `tools/control_tools._CONTROL_ROLES`: all 8 canonical roles — any agent may request HITL or self-mode its session.

**Constants:**

* `tools/execution_tools.TASK_OUTPUT_TRUNC = 2000` — blueprint §5.1 line 293 truncation cap. Re-used by all 3 truncating tools and the BackgroundTaskManager watcher.

**No new state channels. No `core/permissions.py` edits. No new dependencies.** Existing checkpoints deserialize unchanged. `permissions.ToolPrivilegeTier` enum is intentionally NOT extended with a "CONTROL" value — that design intent is honoured behaviourally via READ_ONLY tier (D1 in the approved plan).

---

## Phase 5.7 — Checkpoint Gate Fase 5 (Adversarial E2E Audit) — 2026-05-18

**Strictly additive at the schema level. No state-field renames, removals, or new channels. `core/permissions.py` byte-identical. `brain/state.py` byte-identical.**

Phase 5.7 is the milestone gate that proves the cumulative Phase 5.1–5.6 contract holds under attack. It lands as **one new test file** plus **honest description compression** in 5 Pydantic input models (user-locked D5 remedy, see DEV_JOURNAL.md for the full rationale). No tool semantics or runtime behaviour change.

**New test artefact — `tests/test_phase5_7_checkpoint_gate.py`:** 7 mocked-boundary integration tests:

* **RBWE Zero-Trust** (A1/A2) — `AtomicCodePatchTool` / `FileWriteTool` against an unread path → `rbwe_guard` raises `PermissionDeniedError`, `vfs_write` mock registers 0 calls. The guard call is simulated at the **Node** (orchestrator) boundary rather than the Tool boundary, matching the production reality that LangGraph dispatches `rbwe_guard` upstream of `_arun` (the graph-level integration test that proves the orchestrator wires the guard is deferred to Phase 6 hardening).
* **Tool RAG financial audit** (B1) — load all 14 Phase-5 schemas (5 perception + 3 mutation + 4 execution + 2 control), call `store.select_tools(intent="Run the test suite and check linting", k=TOOL_RAG_TOP_K=5, active_role="core_dev", session_mode=DEFAULT)`, assert `ToolRAGStore.prompt_size_metrics(eager, selected)["reduction_ratio"] >= 0.70`. With the post-compression schemas the empirical ratio is **0.7010** (eager 6054 bytes, selected 1810 bytes).
* **AST malicious-patch containment** (C1/C2) — `replace_block` / `content` that produces unparseable Python → `_validate_python_syntax` raises `PatchError` → tool returns `[atomic_code_patch] ERROR: …` or `[file_write] ERROR: …` → `vfs_write` mock 0 calls.
* **Asymmetric Friction containment** (D1/D2) — `rm -rf node_modules` → `DANGEROUS_COMMANDS_REGEX` blocks before `asyncio.create_subprocess_shell` (subprocess spawn mock raises `AssertionError` if ever called; `spawn_calls == []`); subsequent `AskUserQuestionTool` call populates `state["pending_hitl_request"]` with a 32-char uuid4 hex `request_id`. Sentinel string from the bash interceptor advises the agent to call `ask_user_question`; D2 verifies that escalation does in fact populate the suspension channel.

**Modified — `mypy.ini`:** added `[mypy-tools.patch_tool]` `follow_imports = silent` stanza. Phase 5.7's test file is the first one to transitively pull `tools.patch_tool` into a `mypy --strict` invocation; the pre-existing `unused-ignore` at `patch_tool.py:219` (LangChain `@tool` decorator stub mismatch) surfaced. Same pattern already documented for `agents.planner` and `core.compute_pool` — out-of-scope strict violations get a follow_imports=silent gate so the in-scope file can land clean.

**Modified — Pydantic Field descriptions ONLY (D5 user-locked remedy)** in `tools/mutation_tools.py::AtomicCodePatchInput`, `tools/execution_tools.py::{SandboxBashInput, CheckTypeIntegrityInput}`, `tools/control_tools.py::TogglePlanModeInput`, `tools/perception_tools.py::TraceDataFlowInput`. The first run of B1 reported `reduction_ratio = 0.636`; the locked remedy was to compress verbose descriptions (NOT lower the threshold nor shrink `TOOL_RAG_TOP_K`). Field/class names and `Literal` / `Optional` typing remain unchanged — the operational verbosity ("Phase 1 of the batch rejects…", "Subject to DANGEROUS_COMMANDS_REGEX pre-check", multi-sentence enum explainers) was removed; that detail lives more naturally in the `BaseTool` docstrings (which do NOT enter the measured `json_schema`) or in code comments. No tool semantics changed.

**No new state channels. No new dependencies. No `core/permissions.py` edits. No `brain/state.py` edits.** Phase 5 LOCK-IN (CLAUDE.md §1) auto-expires with the 5.7 `[x]` mark.
---

## Phase 6.3 — OOM Cascade: 6 Operational-Safety state channels — 2026-05-19

**Additive only. No renames, no removals, no Literal narrowing. Phase 5.7 checkpoints deserialize unchanged.**

`brain/state.py::AIlienantGraphState` gains the **6 Phase-6 channels** mandated by `PHASE_6_BLUEPRINT.md` §1 — all scalar overwrite (no reducer), safe defaults. Landed in one shot during Phase 6.3 (the OOM Cascade needs `oom_fallback_active`; the other 5 front-load the 6.4 DLQ / 6.5 Supervisor work and avoid a second `state.py` churn):

* `accumulated_session_cost: float` — USD cost across the WHOLE WebSocket session (many tasks). Owner: `core/supervisor.py` (Phase 6.5).
* `session_max_budget_usd: float` — hard session ceiling injected at graph construction from `AILIENANT_MAX_SESSION_BUDGET_USD`. Owner: task_service (Phase 6.5).
* `oom_fallback_active: bool` — set True by `tools/llm_gateway.py::ainvoke` when it traps an OOM-class exception; read by `brain/nodes/circuit_breaker.py` to bypass the `error_streak` evaluation; reset to False once the fallback turn is acknowledged. **This is the only channel functionally wired in 6.3.**
* `sandbox_tier_active: Literal["DOCKER","WASM","NATIVE_HITL"]` — mirror of `core.sandbox.ACTIVE_TIER` injected at graph start. Owner: graph factory (Phase 6.5).
* `hitl_audit_chain_head: Optional[str]` — blake2b chain_hash of the last resolved `hitl_audit_log` row. Owner: `core/audit.py` (Phase 6.6).
* `dead_letter_episode_id: Optional[str]` — episode_id of a resumed DLQ turn; None for fresh tasks. Owner: `main.py::resume_task` (Phase 6.4).

**No new dependencies. No `core/permissions.py` edits.** `LLMGateway.ainvoke` gains an optional `state: Optional[Dict[str, Any]] = None` parameter (backward compatible — no existing caller breaks; the channel mutation only happens when a caller threads `state`, deferred to a later phase).

---

## Phase 6.4 — DLQ table `dead_letter_tasks` — 2026-05-19

**Additive only. New SQLite table in the catalog DB (`DB_CATALOG_PATH`); no state-channel changes, no renames.** The Phase 6 state channel `dead_letter_episode_id` was already added in Phase 6.3 (Blueprint §1 block) — 6.4 only writes to it via the Resume API.

**New table — `dead_letter_tasks`** (created idempotently by `core/dead_letter.py::init_dlq_table()`, invoked from the FastAPI lifespan after `catalog_db.init_db()`):

```sql
CREATE TABLE IF NOT EXISTS dead_letter_tasks (
    episode_id               TEXT    PRIMARY KEY,   -- uuid4 hex
    task_id                  TEXT    NOT NULL,
    thread_id                TEXT    NOT NULL,      -- == task_id in this codebase
    failed_node              TEXT    NOT NULL,
    exception_class          TEXT    NOT NULL,
    exception_message        TEXT    NOT NULL,      -- truncated to 2000 chars
    state_snapshot_blob_hash TEXT    NOT NULL,      -- blake2b CAS ref (RAM-backed; integrity marker only)
    created_at               INTEGER NOT NULL,
    resolved_at              INTEGER                -- NULL until a successful resume stamps it
);
CREATE INDEX IF NOT EXISTS idx_dlq_task_id ON dead_letter_tasks(task_id, created_at);
```

**`resolved_at` is a deliberate non-ADR addition** to the Blueprint §5.1 DDL: the blueprint's resume idempotency ("task already COMPLETED") is not implementable without it (no task-status table exists). Idempotency is realized as "is there a row with `resolved_at IS NULL` for this `task_id`?" — a successful resume stamps `resolved_at`, so a repeated `POST /api/v1/task/resume/{task_id}` is a deterministic no-op.

**New module-level API — `core/dead_letter.py`:** `DeadLetterRecord` (Pydantic), `init_dlq_table()`, `save_dead_letter(...)`, `get_pending_dlqs(task_id=None)`, `mark_dlq_resolved(episode_id)`, `dead_letter_decorator(node_name)`. **No new dependencies, no `core/db.py` edits, no `ws_contracts.py` edits.** Existing checkpoints deserialize unchanged.

## Phase 6.5 — FinOps Supervisor: no schema change — 2026-05-19

**No new state channels, no new SQLite tables, no renames.** The Phase 6.5 Supervisor (`core/supervisor.py`) is purely additive at the *graph-topology* level and consumes channels that already exist:

* It **reads** `task_id`, `hitl_audit_chain_head`, `session_max_budget_usd`, `security_flags` and **writes** `accumulated_session_cost`, `session_max_budget_usd`, `security_flags` — every one of these was already added in the Phase 6.3 Blueprint §1 block above. The Supervisor is simply the declared owner of `accumulated_session_cost` finally coming online.
* `SESSION_BUDGET_HARD_KILL` is an additive **value** appended to the existing `security_flags` channel (`Annotated[List[str], operator.add]`) — not a new channel.
* `core/audit.py` is created as a **minimal seam** (`AuditChainBrokenError`, `get_chain_head` stub) — no table yet. The `hitl_audit_log` table lands in Phase 6.6.

Existing checkpoints deserialize unchanged.
