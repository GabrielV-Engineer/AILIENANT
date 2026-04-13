"""
AILIENANT CORE - LangGraph Skeleton
Este módulo define la topología del enjambre de agentes y el flujo del estado.
"""

# AILIENANT-core/graph.py 🐜
from typing import Literal
from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from typing import cast
from state import AilienantGraphState, ContextMeter
from config import check_cloud_availability
from checkpoint import checkpoint_manager

# ==========================================
# 1. DEFINICIÓN DE NODOS (AGENT STUBS)
# ==========================================

def planner_node(state: AilienantGraphState) -> AilienantGraphState:
    """
    Planificador: Único nodo autorizado para inicializar/modificar el wbs_plan.
    Complejidad actual: O(1). En el futuro implicará O(T) donde T son los tokens generados.
    """
    print(f"[PlannerAgent] Analizando tarea: {state['task_id']}")
    # Mutación permitida según SCHEMA_EVOLUTION.md
    state["current_step"] = "PLANNING"
    if not state.get("wbs_plan"):
        state["wbs_plan"] = [{"step": 1, "description": "Analizar requerimientos"}]
    
    return state

def orchestrator_node(state: AilienantGraphState) -> AilienantGraphState:
    """
    Orquestador con Conciencia de Entorno (Environment-Aware).
    Verifica disponibilidad de Cloud antes de asignar la ruta.
    """
    print("[OrchestratorAgent] Evaluando CSS y Capacidades del Entorno...")
    state["current_step"] = "ROUTING"
    
    # Verificamos si existe alguna llave de nube configurada
    has_cloud_config = check_cloud_availability()
    
    # SOLUCIÓN 2: Inicialización real. 
    # Si no existe, creamos el OBJETO en la memoria con tu valor de 80.0
    if not state.get("context_metrics"):
        state["context_metrics"] = ContextMeter(css_total=80.0)
    
    # SOLUCIÓN 1: Extracción segura usando el PUNTO de Pydantic
    css_score = state["context_metrics"].css_total 
    
    # --- Lógica de Validación de Capacidades ---
    
    if css_score > 75.0:
        # Asignación usando PUNTO
        state["context_metrics"].routing_decision = "LOCAL_SMALL"
    elif css_score <= 75.0 and has_cloud_config:
        state["context_metrics"].routing_decision = "CLOUD"
    else:
        print("⚠️ [Orchestrator] Cloud requerido por bajo CSS, pero no hay configuración. Usando LOCAL_BIG.")
        state["context_metrics"].routing_decision = "LOCAL_BIG"
        
        # SOLUCIÓN 3: Inicialización segura de la lista de errores antes del append
        if not state.get("errors"):
            state["errors"] = []
        state["errors"].append("Cloud fallback triggered: No API keys found.")

    return state

def logic_node(state: AilienantGraphState) -> AilienantGraphState:
    """
    Lógica: Escribe el código. No puede alterar el plan ni la ruta.
    """
    # Extracción segura usando notación de PUNTO para Pydantic
    routing_decision = state["context_metrics"].routing_decision if state.get("context_metrics") else "UNKNOWN"
    
    print(f"[LogicAgent] Escribiendo código usando modelo: {routing_decision}")
    state["current_step"] = "CODING"
    
    # Inicialización segura (mutación permitida en generated_code)
    if not state.get("generated_code"):
        state["generated_code"] = {}
    
    state["generated_code"]["main.py"] = "# TODO: Implement logic"
    
    return state

# ==========================================
# 2. DEFINICIÓN DE ARISTAS CONDICIONALES
# ==========================================

def route_from_orchestrator(state: AilienantGraphState) -> Literal["logic_node", "__end__"]:
    """
    Smart Router: Decide el próximo salto topológico del grafo basado en el estado.
    Complejidad de ruteo: O(1)
    """
    # Si detectamos una bandera roja de seguridad, abortamos (Fase 4.3).
    if state.get("security_flags"):
        print("⚠️ [Smart Router] Bloqueo de seguridad detectado. Finalizando ejecución.")
        return "__end__"
        
    # En flujo normal, vamos al LogicAgent
    return "logic_node"


# ==========================================
# 3. CONSTRUCCIÓN DEL GRAFO TOPOLÓGICO
# ==========================================

def build_ailienant_graph():
    """
    Construye la Máquina de Estados Finitos (FSM) de Ailienant.
    Retorna el Blueprint (plano) sin compilar para permitir la inyección de persistencia.
    """
    # 1. Instanciación con nuestro contrato estricto
    workflow = StateGraph(AilienantGraphState)

    # 2. Registro de Nodos
    workflow.add_node("planner_node", planner_node)
    workflow.add_node("orchestrator_node", orchestrator_node)
    workflow.add_node("logic_node", logic_node)

    # 3. Definición de Aristas (Edges) y Flujo
    # El punto de entrada es siempre el planificador
    workflow.set_entry_point("planner_node")
    
    # Arista incondicional: Del planificador SIEMPRE pasa al orquestador
    workflow.add_edge("planner_node", "orchestrator_node")
    
    # Arista condicional: El orquestador decide a dónde ir
    workflow.add_conditional_edges(
        "orchestrator_node",
        route_from_orchestrator,
        {
            "logic_node": "logic_node",
            END: END
        }
    )
    
    # Arista incondicional: Al terminar la lógica, el flujo base termina (por ahora)
    workflow.add_edge("logic_node", END)

    # Retornamos el Blueprint sin compilar
    return workflow

# ==========================================
# 4. MOTOR DE EJECUCIÓN CON PERSISTENCIA
# ==========================================

def execute_task_with_memory(task_id: str, initial_state: dict):
    """
    Ejecuta el grafo inyectando SQLite de forma segura (Thread-safe).
    Costo de I/O: O(S) por cada salto de nodo, donde S es el delta del estado.
    
    NOTA: Esta función es el ÚNICO punto de entrada autorizado 
    para ejecutar la IA desde FastAPI o WebSockets.
    """
    # 1. Obtenemos los planos (Blueprint) del grafo limpios
    workflow = build_ailienant_graph()
    
    # 2. Usamos nuestro patrón Context Manager para evitar Memory Leaks
    with checkpoint_manager.get_saver() as saver:
        # Compilación JIT (Just-In-Time) con el checkpointer inyectado
        app = workflow.compile(checkpointer=saver)
        
        # Definición del Thread (La llave mágica del Time-Travel)
        config = cast(RunnableConfig, {"configurable": {"thread_id": task_id}})
        
        print(f"🚀 Ejecutando tarea '{task_id}' con persistencia en SQLite...")
        
        # Ejecución del flujo: Casteamos initial_state para que Pylance entienda que es seguro, 
        result = app.invoke(cast(AilienantGraphState, initial_state), config=config)
        
        return result