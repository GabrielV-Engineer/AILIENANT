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