# SCHEMA_EVOLUTION.md
## LangGraph State Definition para Ialienant

Este documento define el esquema estricto de datos que fluye a través del grafo de LangGraph. Se implementará en Python utilizando `typing.TypedDict` y validación con `pydantic`.

### 🧠 1. Core State Schema (`AIlienantGraphState`)

```python
from typing import TypedDict, List, Dict, Any, Optional
from pydantic import BaseModel

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
    cost_per_1k_tokens: float  # 0.0 para locales, >0 para CLOUD

class EnvironmentProfile(BaseModel):
    os_name: str
    vram_available_gb: float
    # Registro de capacidades. El orquestador lee esto antes de enrutar.
    available_models: Dict[str, LLMProfile] # Map de roles ("LOCAL_SMALL", "CLOUD") a sus perfiles reales

class AgentMemorySnapshot(BaseModel):
    vector_results: List[Dict[str, Any]]  # Snippets de código de LanceDB
    topological_paths: List[str]          # Relaciones de NetworkX en Markdown
    environment: EnvironmentProfile       # Múltiples agentes consumen esta metadata

class AilienantGraphState(TypedDict):
    # 1. Metadatos de la Petición
    task_id: str
    user_input: str
    is_manual_override: bool          # True si el usuario usó el Agent Launcher (Bento Menu)
    target_agent: Optional[str]       # Si is_manual_override es True, indica el agente
    
    # 2. Contexto Dinámico (Llenado por AgentMemory y Orchestrator)
    context_metrics: ContextMeter
    memory_snapshot: AgentMemorySnapshot
    
    # 3. Flujo de Ejecución (LangGraph)
    current_step: str                 # Nodo actual en ejecución
    wbs_plan: List[Dict[str, Any]]    # Work Breakdown Structure (PlannerAgent)
    completed_steps: List[str]
    
    # 4. Artefactos y Modificaciones
    generated_code: Dict[str, str]    # path_archivo -> contenido_nuevo
    terminal_output: Optional[str]    # stdout/stderr de pruebas efímeras
    
    # 5. Telemetría y Seguridad
    tokens_used_local: int
    tokens_used_cloud: int
    security_flags: List[str]         # Alertas del SecOpsAgent
    errors: List[str]                 # Fallos críticos durante el ciclo

```

---

### 📋 2. Reglas de Transición (Mutaciones de Estado)

Para garantizar un flujo predecible y evitar colisiones de datos en LangGraph, los permisos de mutación sobre `AIlienantGraphState` están estrictamente limitados:

* **🧭 PlannerAgent:** Único agente autorizado para inicializar y modificar el arreglo `wbs_plan` y avanzar la lista de `completed_steps`.
* **🧠 OrchestratorAgent:** Único agente autorizado a modificar `context_metrics.routing_decision` y asignar el `target_agent`. No tiene permisos para escribir código.
* **⚙️ LogicAgent:** Tiene permisos de escritura en `generated_code`. NO puede alterar el plan (WBS) ni las decisiones de enrutamiento.
* **🐜 AnalystAgent (La Hormiga):** Operación estricta de **Solo Lectura** sobre `generated_code` y `memory_snapshot`. Su salida va directamente a la UI o añade notas explicativas al estado, pero *nunca* altera el código fuente directamente.
* **🛡️ SecOpsAgent:** Tiene autoridad de bloqueo. Si inyecta un error en `security_flags`, el grafo DEBE pausarse (Human-in-the-Loop) o forzar una transición de regreso al `LogicAgent` para remediación.
* **🧪 TestAgent & 🐛 DebugAgent:** Autorizados para escribir en `terminal_output` y añadir fallos a `errors`. Si los tests fallan, tienen el poder de devolver el estado actual al `LogicAgent` o `RefactorAgent`.
* **🧹 RefactorAgent, 🏗️ InfraAgent & 📖 DocAgent:** Tienen permisos de escritura sobre `generated_code`, pero acotados a sus dominios (optimizaciones de AST, Dockerfiles/ENVs, y archivos `.md`/docstrings, respectivamente).
* **💾 AgentMemory (Motor):** Toda escritura confirmada en `generated_code` debe pasar por la validación de hashing del motor de memoria antes de reflejarse permanentemente en LanceDB/NetworkX.