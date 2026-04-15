# alienant-core/agents/planner.py

import logging
import uuid
from typing import Dict, Any

from langchain_core.messages import HumanMessage, SystemMessage

# Importamos nuestra puerta de enlace y los contratos estrictos
from tools.llm_gateway import LLMGateway
from brain.state import MissionSpecification, WBSStep
from shared.rbac import PLANNER_IDENTITY
from prompts import build_safe_prompt

# Configuración del logger para este nodo específico
logger = logging.getLogger("PLANNER_NODE")

def run_planner_node(state: dict) -> dict:
    """
    Nodo de LangGraph: El Estratega (The Architect & SDD Enforcer).
    
    Misión:
        Analiza el requerimiento del usuario y el contexto del IDE (VFS) para 
        generar un Macro-Contrato estricto (MissionSpecification). No ejecuta código.
    
    Args:
        state (dict): El estado global actual (AIlienantGraphState).
        
    Returns:
        dict: Un diccionario con la actualización parcial del estado.
              Específicamente actualiza la clave 'mission_spec' y opcionalmente 'errors'.
    """
    logger.info("🧠 PlannerAgent iniciando análisis arquitectónico de la misión...")
    
    # =====================================================================
    # 0. MODO SIMULACRO (Circuito Corto para Pruebas UI/Backend)
    # =====================================================================
    DEBUG_MODE = True  # Cambiar a False en Producción para habilitar el LLM
    
    if DEBUG_MODE:
        logger.warning("⚠️ MODO DEBUG ACTIVO: Generando contrato SDD sintético (Bypass de LLM).")
        
        # Generamos un contrato Pydantic real para no romper la validación downstream
        mock_mission = MissionSpecification(
            outcome="Análisis inicial completado de forma sintética.",
            scope=["main.py"],
            constraints=["Sin dependencias externas."],
            decisions=["Usar el modo DEBUG para validar el enrutamiento del grafo."],
            tasks=[
                WBSStep(
                    step_number=1,
                    target_role="Refactor",
                    action="read_file",
                    target_file="main.py",
                    description="Leer archivo principal para validar conexión del IDE.",
                    status="pending"
                )
            ],
            checks=["El archivo se leyó sin lanzar excepciones."]
        )
        return {"mission_spec": mock_mission}

    # =====================================================================
    # 1. EXTRACCIÓN DE CONTEXTO (Input del Usuario e IDE)
    # =====================================================================
    user_input = state.get("user_input", "")
    # Extraemos de forma segura los buffers (puede venir como dict u objeto dependiendo del serializador)
    ide_context = state.get("ide_context", {})
    
    # Compatibilidad: si ide_context es un modelo Pydantic, usamos .dict(), si ya es dict, lo usamos directo.
    dirty_buffers = ide_context.get("dirty_buffers", []) if isinstance(ide_context, dict) else getattr(ide_context, "dirty_buffers", [])

    # =====================================================================
    # 2. 🛡️ BLOQUE DE SEGURIDAD: SANDBOXING INVISIBLE (Defensa contra Prompt Injection)
    # =====================================================================
    # Generamos un candado criptográfico efímero para aislar el código del usuario de las instrucciones del sistema.
    boundary = uuid.uuid4().hex 
    
    context_str = ""
    if not dirty_buffers:
        context_str = f"<{boundary}>No se detectaron archivos sucios ni contexto activo en el IDE.</{boundary}>"
    else:
        for buf in dirty_buffers:
            # Compatibilidad dict vs Pydantic object
            filepath = buf.get("path") if isinstance(buf, dict) else buf.path
            content = buf.get("content") if isinstance(buf, dict) else buf.content
            
            context_str += f'<{boundary} filepath="{filepath}">\n{content}\n</{boundary}>\n\n'

    # =====================================================================
    # 3. CONSTRUCCIÓN DEL PROMPT (RBAC y Spec-Driven Development)
    # =====================================================================
    # Construimos el System Prompt usando el rol estricto del Planner
    system_prompt_text = build_safe_prompt(
        agent_identity=PLANNER_IDENTITY,
        context_str=context_str,
        boundary=boundary
    )
    
    # Instrucción humana: Aquí forzamos mentalmente al modelo a respetar el contrato SDD.
    instruction = (
        f"Requerimiento del usuario: '{user_input}'.\n\n"
        f"Contexto del IDE (Los archivos están encapsulados bajo la etiqueta segura <{boundary}>):\n"
        f"{context_str}\n"
        "Eres el Arquitecto (The Planner). Tienes PROHIBIDO escribir código de implementación.\n"
        "Tu única tarea es generar una especificación técnica completa y lógica (MissionSpecification). "
        "Define de forma estricta el Outcome, Scope, Constraints, Decisions, las Tasks secuenciales (WBS) "
        "asignando un target_role válido ('Refactor', 'Infra', 'Doc', 'SecOps', 'Test') a cada tarea, "
        "y los Checks de validación QA."
    )

    messages = [
        SystemMessage(content=system_prompt_text),
        HumanMessage(content=instruction)
    ]

    # =====================================================================
    # 4. INVOCACIÓN AL MOTOR LLM (Con Validación Pydantic Forzada)
    # =====================================================================
    llm = LLMGateway.get_model() 
    
    # Obligamos a la IA a devolver la estructura exacta de nuestro Macro-Contrato.
    structured_llm = llm.with_structured_output(MissionSpecification, include_raw=True)
    
    logger.info("⏳ Esperando Especificación Técnica (SDD) del LLM...")
    
    try:
        response_container = structured_llm.invoke(messages)
        raw_parsed = response_container.get("parsed")

        # Defensa de Nivel 1: El LLM falló por completo en generar un JSON válido.
        if raw_parsed is None:
            error_msg = response_container.get("parsing_error", "Error desconocido en parsing estructurado.")
            logger.error(f"❌ Error de parsing del LLM: {error_msg}")
            # Mutamos la clave 'errors' en el estado global para que el Orchestrator aplique reintentos.
            return {"errors": [f"El LLM no generó un contrato SDD válido: {error_msg}"]}

        # Defensa de Nivel 2: Type Guarding para estabilidad del IDE y LangGraph.
        if isinstance(raw_parsed, dict):
            mission_plan = MissionSpecification(**raw_parsed)
        elif isinstance(raw_parsed, MissionSpecification):
            mission_plan = raw_parsed
        else:
            raise TypeError("El LLM devolvió un tipo de dato irreconocible que rompe el contrato SDD.")

        # =====================================================================
        # 5. AUDITORÍA Y ACTUALIZACIÓN DEL ESTADO GLOBAL
        # =====================================================================
        logger.info("✅ Especificación Técnica generada y validada estrictamente.")
        
        print(f"\n--- 📋 MISSION SPECIFICATION (SDD) ---")
        print(f"🎯 Outcome: {mission_plan.outcome}")
        print(f"🔒 Constraints: {len(mission_plan.constraints)} reglas definidas.")
        print(f"🛠️ Pasos (Tasks): {len(mission_plan.tasks)} tareas programadas.")
        print(f"🧪 Checks (QA): {len(mission_plan.checks)} pruebas de validación.")
        print(f"--------------------------------------\n")
        
        # Mapeo Directo: Actualizamos el 'mission_spec' del AIlienantGraphState.
        # Al no devolver listas sueltas, evitamos el riesgo de desincronización de estado.
        return {
            "mission_spec": mission_plan
        }
        
    except Exception as e:
        logger.error(f"❌ Error crítico en la ejecución del Planner: {str(e)}")
        # Inyectamos el error en el estado para activar protocolos de auto-recuperación (Resiliencia)
        return {"errors": [f"Planner Error - Fallo en generación SDD: {str(e)}"]}