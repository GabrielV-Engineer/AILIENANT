# ailienant-core/brain/state.py

import operator
from typing import TypedDict, List, Dict, Optional, Annotated, Literal
from pydantic import BaseModel, Field

from shared.hardware import HardwareProfile  # noqa: E402 — imported for type annotation

# =====================================================================
# 1. MODELOS DE DATOS (Contratos de Validación Estricta en Tiempo de Ejecución)
# =====================================================================
# Utilizamos Pydantic para aplicar el Fail-Fast Principle. Si el LLM (Planner)
# alucina la estructura del JSON, el sistema fallará y reintentará inmediatamente,
# evitando propagar datos corruptos al Orchestrator o al CoderAgent.


class WBSStep(BaseModel):
    """
    Un paso individual, atómico y ejecutable de la misión.
    Refactor (Fase 4): Integra su propio 'status' y reemplaza agentes por roles dinámicos.
    """

    step_number: int = Field(
        description="El orden secuencial de ejecución (1, 2, 3...)."
    )
    target_role: Literal["Refactor", "Infra", "Doc", "SecOps", "Test"] = Field(
        default="Refactor",
        description="El rol ('System Prompt') que el CoderAgent debe asumir para ejecutar esta tarea.",
    )
    action: Literal["read_file", "write_file", "edit_file", "run_command"] = Field(
        description="Tipo de acción estricta permitida para este paso."
    )
    target_file: str = Field(
        description="Ruta exacta del archivo afectado (ej. 'src/routes/auth.py') o comando a ejecutar."
    )
    description: str = Field(
        description="Instrucción detallada de lo que el CoderAgent debe hacer en este paso."
    )
    status: Literal["pending", "in_progress", "completed", "failed"] = Field(
        default="pending",
        description="Estado actual de la tarea. El Orchestrator muta esto durante la ejecución.",
    )


class MissionSpecification(BaseModel):
    """
    EL MACRO-CONTRATO (Spec-Driven Development).
    Forza al PlannerAgent a definir la arquitectura completa antes de escribir una línea de código.
    """

    outcome: str = Field(
        description="El resultado final esperado y el valor aportado por esta misión."
    )
    scope: List[str] = Field(
        description="Definición estricta de lo que está DENTRO y FUERA del alcance. Qué archivos tocar y cuáles NO."
    )
    constraints: List[str] = Field(
        description="Limitaciones técnicas (ej. sin librerías externas, complejidad O(n), convenciones del proyecto)."
    )
    decisions: List[str] = Field(
        description="Decisiones de diseño o arquitectura adoptadas para resolver este problema en particular."
    )
    tasks: List[WBSStep] = Field(
        description="Work Breakdown Structure (WBS). La lista secuencial y estricta de pasos a ejecutar."
    )
    checks: List[str] = Field(
        description="Criterios de aceptación técnicos. ¿Cómo sabrá el micro-enjambre de Testing que la tarea fue un éxito?"
    )


class ContextMeter(BaseModel):
    """Telemetría para el motor de enrutamiento 3D (Local vs Cloud)."""

    semantic_similarity: float = Field(
        ge=0.0, le=1.0, description="Score de similitud semántica (LanceDB)."
    )
    graph_coverage: float = Field(
        ge=0.0, le=1.0, description="Cobertura del grafo de dependencias (NetworkX)."
    )
    recency_score: float = Field(
        ge=0.0, le=1.0, description="Peso basado en archivos modificados recientemente."
    )
    css_total: float = Field(
        ge=0.0,
        le=100.0,
        description="Context Sufficiency Score (Métrica global de contexto).",
    )
    task_complexity_index: float = Field(
        ge=0.0, le=100.0, description="Índice de complejidad calculado de la tarea."
    )
    routing_decision: str = Field(
        pattern="^(LOCAL_SMALL|LOCAL_BIG|CLOUD)$", description="Decisión del enrutador."
    )
    is_red_alert: bool = Field(
        description="True si el CSS es críticamente bajo (<40%)."
    )


class LLMProfile(BaseModel):
    """Firma del modelo actualmente en ejecución."""

    model_name: str
    parameters_b: float
    context_window: int
    quantization: str


class TokenCounter(BaseModel):
    """Auditoría de uso y costos."""

    local: int = 0
    cloud: int = 0
    total_cost_usd: float = 0.0


class VFSFile(BaseModel):
    """Representa un archivo en memoria con control de concurrencia (Virtual File System)."""

    content: str = Field(..., description="Contenido en texto plano del archivo.")
    document_version_id: str = Field(
        ...,
        description="Timestamp o Hash MD5 para OCC (Optimistic Concurrency Control).",
    )
    is_dirty: bool = Field(
        default=False,
        description="True si la IA lo modificó y falta sincronizar al IDE del usuario.",
    )


class ManualAttachment(BaseModel):
    """Contexto multimodal inyectado manualmente por el usuario (imagen o documento)."""

    type: Literal["image", "document"]
    data: Optional[str] = Field(
        None,
        max_length=10_485_760,  # 10 MB ceiling on base64 payload to prevent OOM
        description="Bytes codificados en base64 (solo imágenes).",
    )
    content: Optional[str] = Field(None, description="Texto plano del documento.")
    mime: Optional[str] = Field(None, description="Tipo MIME, e.g. 'image/png'.")
    name: Optional[str] = Field(None, description="Nombre del archivo adjunto.")


# =====================================================================
# 2. ESTADO DEL GRAFO (AIlienant Context) (LangGraph TypedDict)
# =====================================================================


def _merge_generated_code(
    left: Dict[str, "VFSFile"], right: Dict[str, "VFSFile"]
) -> Dict[str, "VFSFile"]:
    """Reducer for parallel CoderAgent output buffers. Keeps the entry with the
    lexicographically later document_version_id so the most recent generated edit
    survives multi-agent fan-out collisions."""
    merged = dict(left)
    for path, file in right.items():
        if path not in merged or file.document_version_id > merged[path].document_version_id:
            merged[path] = file
    return merged


def _merge_vfs(left: Dict[str, "VFSFile"], right: Dict[str, "VFSFile"]) -> Dict[str, "VFSFile"]:
    """Reducer for concurrent CoderAgent VFS writes. Keeps the file with the
    lexicographically later document_version_id (timestamp/hash), preventing
    a slower parallel branch from overwriting a more recent edit."""
    merged = dict(left)
    for path, file in right.items():
        if path not in merged or file.document_version_id > merged[path].document_version_id:
            merged[path] = file
    return merged


class AIlienantGraphState(TypedDict):
    """
    El cerebro compartido del flujo de LangGraph.
    Define estrictamente la memoria y variables que los nodos pueden leer o mutar.
    """

    # --- Identidad de la Misión ---
    task_id: str
    user_input: str

    # --- Workspace Identity & Manual Context (Phase 1.1.0 / 1.1.0.4) ---
    project_id: Optional[str]              # SHA-256 of the VS Code workspace root path
    explicit_mentions: List[str]           # @-referenced file paths → forced full-file read
    attachments: List[ManualAttachment]    # user-attached images / documents

    # --- Memoria de Mensajes ---
    # Historial acumulativo O(N) para la comunicación conversacional.
    messages: Annotated[List[Dict[str, str]], operator.add]

    # --- Contexto y Telemetría ---
    context_metrics: ContextMeter
    active_llm_profile: LLMProfile
    token_usage: TokenCounter

    # --- Control de Flujo (Prompt Swapping) ---
    is_manual_override: bool
    target_role: Optional[
        str
    ]  # Sustituye a 'target_agent'. Define el rol actual del CoderAgent.
    current_step_id: Optional[
        int
    ]  # Puntero a la tarea actual del WBS en ejecución (step_number).

    # --- Human-in-the-Loop & Planner Mode (Phase 1.4) ---
    planner_mode_active: bool       # True when user toggled Planner-only mode via WS event
    hitl_pending: bool              # True while the graph is awaiting human approval
    hitl_response: Optional[str]   # "approved" | "rejected" + optional comment from HITL response

    # --- Planificación Inmutable (SDD) ---
    # Reemplaza 'immutable_wbs' y 'completed_steps'.
    # Todo el estado del plan vive dentro de este único objeto para evitar desincronizaciones.
    mission_spec: Optional[MissionSpecification]

    # --- Sistema de Archivos Virtual (VFS) ---
    # Single Source of Truth para el código.
    read_files_state: Dict[str, VFSFile]
    vfs_buffer: Annotated[Dict[str, VFSFile], _merge_vfs]

    # --- Enrutamiento MoE (Phase 2) ---
    # Shortcuts para que los nodos de orquestación lean TCI/CSS sin navegar context_metrics.
    tci: float          # Task Complexity Index  0–100
    css: float          # Context Sufficiency Score  0–100
    # Payload del fan-out MapReduce: PlannerAgent escribe, route_to_coders lee.
    parallel_tasks: List[WBSStep]

    # --- Routing & Hardware (Phase 2.1) ---
    # True when at least one attachment is type="image" → forces CLOUD via Vision Bypass.
    has_images: bool
    # Set by resolve_provider() when CLOUD is optimal but unavailable; None otherwise.
    routing_warning: Optional[str]
    # Populated by orchestrator node on first invocation; cached in checkpoint state.
    hardware_profile: Optional[HardwareProfile]
    # Active routing decision written by the orchestrator; read by route_to_coders.
    provider: str
    # Parallel CoderAgent output buffer; _merge_generated_code prevents fan-out collisions.
    generated_code: Annotated[Dict[str, VFSFile], _merge_generated_code]

    # --- Resiliencia y Diagnóstico ---
    errors: Annotated[List[str], operator.add]
    retry_count: int
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str
    # Phase 2.5: Workspace Indexing Gate — seeded from lazy_indexer.is_complete at graph invocation
    is_indexing_complete: bool
