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