# core/state.py
from typing import TypedDict, List, Dict, Any, Optional
from pydantic import BaseModel, Field
import os

class ContextMeter(BaseModel):
    """
    Representa el 'Context Sufficiency Score' (CSS).
    Determina si el sistema tiene suficiente información local para proceder.
    """
    semantic_similarity: float = 0.0
    graph_coverage: float = 0.0
    recency_score: float = 0.0
    css_total: float = 0.0
    routing_decision: str = "LOCAL_SMALL"
    is_red_alert: bool = False

class AgentMemorySnapshot(BaseModel):
    """
    Contenedor de datos extraídos de la memoria GraphRAG para alimentar a los LLMs.
    """
    vector_results: List[Dict[str, Any]] = []
    topological_paths: List[str] = []
    environment_profile: Dict[str, str] = {}

class AilienantGraphState(TypedDict):
    """
    Esquema principal de LangGraph. Define qué datos persisten entre nodos.
    """
    # --- Identificación ---
    task_id: str
    user_input: str
    # --- Control de Flujo (Bento Menu) ---
    is_manual_override: bool          # True si el usuario saltó la orquestación automática
    target_agent: Optional[str]       # Agente específico si is_manual_override es True
    # --- Contexto Dinámico ---
    context_metrics: ContextMeter     # Métricas de salud del contexto
    memory_snapshot: AgentMemorySnapshot # Datos de GraphRAG inyectados
    # --- Gestión del Plan (WBS) ---
    current_step: str                 # ID del paso actual en ejecución
    wbs_plan: List[Dict[str, Any]]    # Plan maestro generado por el PlannerAgent
    completed_steps: List[str]        # Historial de hitos alcanzados
    # --- Artefactos de Salida ---
    generated_code: Dict[str, str]    # Map de: "path/al/archivo": "contenido"
    terminal_output: Optional[str]    # Resultados de tests o ejecuciones efímeras
    # --- Telemetría y Seguridad ---
    tokens_used_local: int            # Conteo de tokens en modelos locales (Qwen/Llama)
    tokens_used_cloud: int            # Conteo de tokens en modelos Cloud (Claude/GPT)
    security_flags: List[str]         # Alertas inyectadas por SecOpsAgent
    errors: List[str]                 # Fallos críticos que activan el Circuit Breaker
    