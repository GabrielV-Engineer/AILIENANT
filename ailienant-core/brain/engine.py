# alienant-core/core/engine.py

import sqlite3
import logging
from typing import Callable, Optional
from langgraph.graph import StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver

# Importamos nuestro esquema fuertemente tipado
from state import AIlienantGraphState

logger = logging.getLogger("AILIENANT_ENGINE")

# =====================================================================
# 1. INICIALIZACIÓN DEL GRAFO
# =====================================================================
# Le indicamos a LangGraph que use nuestro estado con Uniones O(1) y Pydantic
workflow = StateGraph(AIlienantGraphState)

# =====================================================================
# 2. CONFIGURACIÓN DE PERSISTENCIA (CHECKPOINTER)
# =====================================================================
# Usamos SQLite porque es ligero, no requiere contenedores extra en esta fase,
# y maneja lecturas O(1) de manera eficiente para el estado del hilo (thread_id).
DB_PATH = "alienant_memory.sqlite"

try:
    # check_same_thread=False es vital porque FastAPI es asíncrono y multi-hilo
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    memory_checkpointer = SqliteSaver(conn)
    logger.info("🟢 Motor SQLite conectado exitosamente.")
except Exception as e:
    logger.critical(f"🔴 Error fatal conectando al Checkpointer: {e}")
    raise e

# =====================================================================
# 3. COMPILACIÓN DEL MOTOR (PREPARACIÓN FASE 1)
# =====================================================================
# En la Fase 1, añadiremos los nodos (PlannerAgent, LogicAgent) aquí.
# Por ahora, simplemente compilamos un grafo vacío con memoria para probar.

# alienant_app = workflow.compile(checkpointer=memory_checkpointer)
# (Lo mantenemos comentado hasta que añadamos el primer nodo)


# =====================================================================
# 4. CONTEXT ASSEMBLY UTILITIES (Phase 1.1.0.4)
# =====================================================================


def resolve_explicit_mentions(
    explicit_mentions: list[str],
    vfs_read: Callable[[str], Optional[str]],
    boundary: str,
) -> str:
    """Read full content for @-mentioned files, bypassing the GraphRAG threshold.

    Takes a callable so this function stays decoupled from VFSMiddleware and is
    unit-testable with a simple lambda. Callers pass `vfs_instance.read`.

    Logs 'RAG bypass: full-file injection → <path>' for each successful read so
    the DoD log check can be verified without a running graph.
    """
    parts: list[str] = []
    for path in explicit_mentions:
        content = vfs_read(path)
        if content is not None:
            logger.info("RAG bypass: full-file injection → %s", path)
            parts.append(
                f'<{boundary} filepath="{path}" source="explicit_mention">\n{content}\n</{boundary}>'
            )
        else:
            logger.warning("explicit_mention not found in VFS or disk: %s", path)
    return "\n\n".join(parts)
