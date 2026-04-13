# core/state.py
# alienant-core/core/state.py

import operator
from typing import TypedDict, List, Dict, Any, Optional, Annotated
from pydantic import BaseModel, Field

# =====================================================================
# 1. MODELOS DE DATOS (Contratos de Validación Estricta en Tiempo de Ejecución)
# =====================================================================
# Usamos Pydantic porque TypedDict es solo para el linter estático (Mypy).
# En un sistema multi-agente, si el LLM alucina y devuelve un string en vez 
# de un float para 'semantic_similarity', Pydantic lanzará un error inmediato,
# evitando que el error se propague aguas abajo (Fail-Fast Principle).

class WBSStep(BaseModel):
    """Define un paso atómico del Plan Maestro."""
    step_id: str = Field(..., description="ID único ej: 'AUTH_001'")
    description: str
    target_agent: str = Field(..., description="Agente responsable (Logic, Infra, etc.)")
    estimated_tokens: Optional[int] = 0

class ContextMeter(BaseModel):
    semantic_similarity: float = Field(ge=0.0, le=1.0, description="Score de similitud semántica")
    graph_coverage: float = Field(ge=0.0, le=1.0)
    recency_score: float = Field(ge=0.0, le=1.0)
    css_total: float = Field(ge=0.0, le=100.0, description="Context Sufficiency Score")
    task_complexity_index: float = Field(ge=0.0, le=100.0, description="TCI")
    routing_decision: str = Field(pattern="^(LOCAL_SMALL|LOCAL_BIG|CLOUD)$")
    is_red_alert: bool

class LLMProfile(BaseModel):
    model_name: str
    parameters_b: float
    context_window: int
    quantization: str
    
class TokenCounter(BaseModel):
    local: int = 0
    cloud: int = 0
    total_cost_usd: float = 0.0 # Valor añadido para auditoría

# =====================================================================
# 2. ESTADO DEL GRAFO (AIlienant Context) (LangGraph TypedDict)
# =====================================================================

class AIlienantGraphState(TypedDict):
    # --- Memoria de Mensajes ---
    # Historial acumulativo O(N)
    messages: Annotated[List[Dict[str, str]], operator.add]
    
    # --- Contexto y Telemetría ---
    current_task_spec: str
    context_metrics: ContextMeter
    active_llm_profile: LLMProfile
    token_usage: TokenCounter
    
    # --- Control de Flujo ---
    is_manual_override: bool 
    target_agent: Optional[str] 
    
    # --- Planificación (WBS) ---
    # immutable_wbs ahora usa el modelo WBSStep para evitar datos basura
    immutable_wbs: List[WBSStep] 
    completed_steps: Annotated[List[str], operator.add] 
    
    # --- Sistema de Archivos Virtual (VFS) ---
    # Single Source of Truth para el código. 
    # Clave: Ruta del archivo, Valor: Contenido
    read_files_state: Dict[str, str]
    vfs_buffer: Dict[str, str] 
    
    # --- Resiliencia y Diagnóstico ---
    errors: Annotated[List[str], operator.add]
    retry_count: int
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str