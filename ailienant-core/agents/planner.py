# alienant-core/agents/planner.py

import logging
import uuid

# Importamos nuestra puerta de enlace y los contratos estrictos
from tools.llm_gateway import LLMGateway
from shared.config import MODEL_MEDIUM
from brain.state import MissionSpecification, WBSStep, ContextMeter
from shared.rbac import PLANNER_IDENTITY
from prompts import build_safe_prompt
from core.utils import is_polyglot_file
from core.rules import rule_manager
from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
from core.memory.trajectory_memory import TrajectoryMemoryManager, format_trajectories_for_prompt
from core.memory.semantic_memory import SemanticMemoryManager
from core.memory.context_auditor import (
    audit_task_complexity,
    derive_routing_decision,
    RiskLevel,
)

# Configuración del logger para este nodo específico
logger = logging.getLogger("PLANNER_NODE")

_POLYGLOT_WARNING = (
    " [!] POLYGLOT FILE DETECTED: {target_file}. "
    "You MUST use the 'patch_file' tool for any modifications. "
    "Full file rewrites are strictly forbidden to prevent corrupting mixed syntax."
)


def _inject_polyglot_constraints(tasks: list) -> list:
    """Return a new task list with polyglot-file constraints appended to step descriptions."""
    result = []
    for step in tasks:
        if step.target_file and is_polyglot_file(step.target_file):
            step = step.model_copy(update={
                "description": step.description
                    + _POLYGLOT_WARNING.format(target_file=step.target_file)
            })
        result.append(step)
    return result


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

    # Phase 3.0 BFS removed in Phase 3.2 — replaced by semantic-guided deep parse (production path below).
    updated_context_metrics = metrics  # default: pass through unchanged

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

        tasks = _inject_polyglot_constraints(tasks)   # Phase 2.22.6

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
        result: dict = {
            "mission_spec": mock_mission,
            "parallel_tasks": parallel_tasks,
            "tci": tci,
            "css": css,
            "context_metrics": updated_context_metrics,
        }
        # Freeze the baseline on the first turn (immutable_wbs absent or None).
        # DriftMonitor will compare future re-plans against this anchor.
        if state.get("immutable_wbs") is None:
            result["immutable_wbs"] = mock_mission
            logger.info("PlannerAgent: immutable_wbs frozen (first turn, DEBUG mode).")
        return result

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

    _rules = rule_manager.get_combined_rules(state.get("workspace_root", ""))
    if _rules:
        system_prompt_text += f"\n\n{_rules}"

    # ── Phase 3.0.1: Trajectory Memory Injection ────────────────────────
    _traj_mgr = TrajectoryMemoryManager()
    _past_trajectories = await _traj_mgr.search(
        user_input=user_input,
        project_id=state.get("project_id") or "",
    )
    if _past_trajectories:
        system_prompt_text += f"\n\n{format_trajectories_for_prompt(_past_trajectories)}"
        logger.info(
            "TrajectoryMemory: injected %d past trajectories into planner context.",
            len(_past_trajectories),
        )
    # ────────────────────────────────────────────────────────────────────

    # ── Phase 3.2: Semantic-Guided Deep Context Extraction ─────────────────────────
    # Single embedding call returns Top-K file paths + similarity score.
    # deep_parse: 1-degree SQLite neighbor expansion → VFS read → Tree-sitter (in thread).
    # CSS is fully recomputed here; Phase 3.1 block is subsumed.
    if updated_context_metrics is not None:
        try:
            _sem_mgr = SemanticMemoryManager()
            _sem_score: float
            _top_k_files: list[str]
            _sem_score, _top_k_files = await _sem_mgr.search_with_paths(
                user_input=user_input,
                workspace_hash=state.get("project_id") or "",
            )
            _extractor = GraphRAGDynamicExtractor(project_id=state.get("project_id") or "")
            _deep_result = await _extractor.deep_parse(
                seed_files=_top_k_files,
                workspace_root=state.get("workspace_root", ""),
            )
            _new_css: float = min(100.0, max(0.0, (
                0.5 * _sem_score
                + 0.3 * _deep_result.coverage_ratio
                + 0.2 * updated_context_metrics.recency_score
            ) * 100.0))
            updated_context_metrics = updated_context_metrics.model_copy(
                update={
                    "semantic_similarity": _sem_score,
                    "graph_coverage": _deep_result.coverage_ratio,
                    "css_total": _new_css,
                    "is_red_alert": _new_css < 40.0,
                }
            )
            css = _new_css
            if _deep_result.context_block:
                system_prompt_text += f"\n\n{_deep_result.context_block}"
            logger.info(
                "Phase 3.2: sem=%.4f graph=%.3f css=%.1f files_parsed=%d/%d",
                _sem_score, _deep_result.coverage_ratio, _new_css,
                len(_deep_result.parsed_files), len(_deep_result.target_files),
            )
        except Exception as _ctx_err:
            logger.warning("Phase 3.2: context extraction failed (non-fatal): %s", _ctx_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    # ── Phase 3.3: Context Meter Cascade (Early Exit + Mini-Judge) ──────────────────
    _cascade_routing: str = "LOCAL_SMALL"   # conservative safe default
    _cascade_provider: str = "LOCAL"        # safe default

    try:
        # Initialize ContextMeter on first invocation (context_metrics absent from state).
        if updated_context_metrics is None:
            updated_context_metrics = ContextMeter(
                semantic_similarity=0.0,
                graph_coverage=0.0,
                recency_score=0.5,
                css_total=css,
                task_complexity_index=tci,
                routing_decision="LOCAL_SMALL",  # placeholder; overwritten below
                is_red_alert=css < 40.0,
            )

        if updated_context_metrics.is_red_alert:
            # O(1) early exit: context gap → bypass Mini-Judge, force CLOUD.
            _cascade_routing = "CLOUD"
            _cascade_provider = "CLOUD"
            logger.warning(
                "Phase 3.3: RED ALERT (CSS=%.1f) — Mini-Judge bypassed, routing=CLOUD.",
                updated_context_metrics.css_total,
            )
        else:
            _risk: RiskLevel = await audit_task_complexity(user_input, session_id=session_id)
            _math_routing: str = derive_routing_decision(
                tci, updated_context_metrics.css_total
            )

            if _risk == RiskLevel.HIGH:
                _cascade_routing = "CLOUD"
                _cascade_provider = "CLOUD"
                if _math_routing != "CLOUD":
                    logger.warning(
                        "VETO: Semantic risk detected, overriding %s to CLOUD",
                        _math_routing,
                    )
                tci = 100.0
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"task_complexity_index": 100.0}
                )
                logger.info("Phase 3.3.3: MiniJudge=HIGH → TCI=100.0, routing=CLOUD.")

            elif _risk == RiskLevel.MEDIUM:
                if _math_routing == "LOCAL_SMALL":
                    _cascade_routing = "LOCAL_BIG"
                    logger.warning(
                        "VETO: Semantic risk detected, overriding LOCAL_SMALL to LOCAL_BIG"
                    )
                else:
                    _cascade_routing = _math_routing
                _cascade_provider = "CLOUD" if _cascade_routing == "CLOUD" else "LOCAL"
                tci = max(tci, 75.0)
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"task_complexity_index": tci}
                )
                logger.info(
                    "Phase 3.3.3: MiniJudge=MEDIUM → TCI=%.1f, routing=%s.",
                    tci, _cascade_routing,
                )

            else:
                _cascade_routing = _math_routing
                _cascade_provider = "CLOUD" if _cascade_routing == "CLOUD" else "LOCAL"
                logger.info(
                    "Phase 3.3.3: MiniJudge=NONE → routing=%s (math defer).",
                    _cascade_routing,
                )

        updated_context_metrics = updated_context_metrics.model_copy(
            update={"routing_decision": _cascade_routing}
        )
        logger.info(
            "Phase 3.3: cascade done — routing=%s provider=%s css=%.1f tci=%.1f",
            _cascade_routing, _cascade_provider, updated_context_metrics.css_total, tci,
        )
    except Exception as _cascade_err:
        logger.warning("Phase 3.3: cascade failed (non-fatal): %s", _cascade_err)
    # ────────────────────────────────────────────────────────────────────────────────────

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
            mission_plan = mission_plan.model_copy(update={   # Phase 2.22.6
                "tasks": _inject_polyglot_constraints(list(mission_plan.tasks))
            })
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

        result = {
            "mission_spec": mission_plan,
            "parallel_tasks": parallel_tasks,
            "tci": tci,
            "css": css,
            "context_metrics": updated_context_metrics,
            "provider": _cascade_provider,
        }
        if state.get("immutable_wbs") is None:
            result["immutable_wbs"] = mission_plan
            logger.info("PlannerAgent: immutable_wbs frozen (first turn, LLM mode).")
        return result

    except Exception as e:
        logger.error(f"❌ Error crítico en la ejecución del Planner: {str(e)}")
        # Inyectamos el error en el estado para activar protocolos de auto-recuperación (Resiliencia)
        return {"errors": [f"Planner Error - Fallo en generación SDD: {str(e)}"]}
