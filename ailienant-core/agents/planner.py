# alienant-core/agents/planner.py

import logging
import uuid

# Importamos nuestra puerta de enlace y los contratos estrictos
from tools.llm_gateway import LLMGateway
from shared.config import MODEL_MEDIUM
from brain.state import MissionSpecification, WBSStep
from shared.rbac import PLANNER_IDENTITY
from prompts import build_safe_prompt

# Configuración del logger para este nodo específico
logger = logging.getLogger("PLANNER_NODE")


async def run_planner_node(state: dict) -> dict:
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
    # Prefer task_id (AIlienantGraphState) then session_id (loose dict); fall back to uuid4.
    session_id: str = (
        state.get("task_id") or state.get("session_id") or str(uuid.uuid4())
    )

    # =====================================================================
    # 0. MODO SIMULACRO (Circuito Corto para Pruebas UI/Backend)
    # =====================================================================
    DEBUG_MODE = True  # Cambiar a False en Producción para habilitar el LLM

    # Leemos TCI y CSS del estado. Preferimos los shortcuts de top-level (Phase 2);
    # si no están presentes aún, navegamos context_metrics como fallback seguro.
    tci: float = state.get("tci", 0.0)
    css: float = state.get("css", 100.0)
    metrics = state.get("context_metrics")
    if metrics is not None and tci == 0.0:
        tci = getattr(metrics, "task_complexity_index", 0.0)
    if metrics is not None and css == 100.0:
        css = getattr(metrics, "css_total", 100.0)

    if DEBUG_MODE:
        logger.warning(
            "⚠️ MODO DEBUG ACTIVO: Generando contrato SDD sintético (Bypass de LLM). TCI=%.1f CSS=%.1f",
            tci,
            css,
        )

        # Para High-TCI (>80), generamos dos tareas independientes para ejercitar el fan-out.
        if tci > 80.0:
            tasks = [
                WBSStep(
                    step_number=1,
                    target_role="Refactor",
                    action="read_file",
                    target_file="main.py",
                    description="Paso paralelo A: leer archivo principal.",
                    status="pending",
                ),
                WBSStep(
                    step_number=2,
                    target_role="Test",
                    action="read_file",
                    target_file="requirements.txt",
                    description="Paso paralelo B: auditar dependencias.",
                    status="pending",
                ),
            ]
            logger.info("🔀 High-TCI detectado: %d tareas paralelas generadas.", len(tasks))
        else:
            tasks = [
                WBSStep(
                    step_number=1,
                    target_role="Refactor",
                    action="read_file",
                    target_file="main.py",
                    description="Leer archivo principal para validar conexión del IDE.",
                    status="pending",
                )
            ]

        mock_mission = MissionSpecification(
            outcome="Análisis inicial completado de forma sintética.",
            scope=["main.py"],
            constraints=["Sin dependencias externas."],
            decisions=["Usar el modo DEBUG para validar el enrutamiento del grafo."],
            tasks=tasks,
            checks=["El archivo se leyó sin lanzar excepciones."],
        )

        # Extraemos parallel_tasks para High-TCI: todos los pasos son candidatos al fan-out.
        parallel_tasks = mock_mission.tasks if tci > 80.0 else []
        return {
            "mission_spec": mock_mission,
            "parallel_tasks": parallel_tasks,
            "tci": tci,
            "css": css,
        }

    # =====================================================================
    # 1. EXTRACCIÓN DE CONTEXTO (Input del Usuario e IDE)
    # =====================================================================
    user_input = state.get("user_input", "")
    # Extraemos de forma segura los buffers (puede venir como dict u objeto dependiendo del serializador)
    ide_context = state.get("ide_context", {})

    # Compatibilidad: si ide_context es un modelo Pydantic, usamos .dict(), si ya es dict, lo usamos directo.
    dirty_buffers = (
        ide_context.get("dirty_buffers", [])
        if isinstance(ide_context, dict)
        else getattr(ide_context, "dirty_buffers", [])
    )

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

            context_str += (
                f'<{boundary} filepath="{filepath}">\n{content}\n</{boundary}>\n\n'
            )

    # =====================================================================
    # 3. CONSTRUCCIÓN DEL PROMPT (RBAC y Spec-Driven Development)
    # =====================================================================
    # Construimos el System Prompt usando el rol estricto del Planner
    system_prompt_text = build_safe_prompt(
        agent_identity=PLANNER_IDENTITY, context_str=context_str, boundary=boundary
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
        {"role": "system", "content": system_prompt_text},
        {"role": "user", "content": instruction},
    ]

    # =====================================================================
    # 4. INVOCACIÓN AL MOTOR LLM (Con Validación Pydantic Forzada)
    # =====================================================================
    logger.info("⏳ Esperando Especificación Técnica (SDD) del LLM...")

    try:
        response = await LLMGateway.ainvoke(
            messages=messages,
            model=MODEL_MEDIUM,
            temperature=0.0,
            response_format={"type": "json_object"},
            session_id=session_id,
        )
        raw_content = response.choices[0].message.content or ""
        raw_json = LLMGateway._sanitize_json_response(raw_content)

        try:
            mission_plan = MissionSpecification.model_validate_json(raw_json)
        except Exception as parse_err:
            logger.error(f"❌ Error de parsing del LLM: {parse_err}")
            return {"errors": [f"El LLM no generó un contrato SDD válido: {parse_err}"]}

        # =====================================================================
        # 5. AUDITORÍA Y ACTUALIZACIÓN DEL ESTADO GLOBAL
        # =====================================================================
        logger.info("✅ Especificación Técnica generada y validada estrictamente.")

        print("\n--- 📋 MISSION SPECIFICATION (SDD) ---")
        print(f"🎯 Outcome: {mission_plan.outcome}")
        print(f"🔒 Constraints: {len(mission_plan.constraints)} reglas definidas.")
        print(f"🛠️ Pasos (Tasks): {len(mission_plan.tasks)} tareas programadas.")
        print(f"🧪 Checks (QA): {len(mission_plan.checks)} pruebas de validación.")
        print("--------------------------------------\n")

        # Para High-TCI, todos los WBSSteps son candidatos al fan-out MapReduce.
        parallel_tasks = mission_plan.tasks if tci > 80.0 else []

        return {
            "mission_spec": mission_plan,
            "parallel_tasks": parallel_tasks,
            "tci": tci,
            "css": css,
        }

    except Exception as e:
        logger.error(f"❌ Error crítico en la ejecución del Planner: {str(e)}")
        # Inyectamos el error en el estado para activar protocolos de auto-recuperación (Resiliencia)
        return {"errors": [f"Planner Error - Fallo en generación SDD: {str(e)}"]}
