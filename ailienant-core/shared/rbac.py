# alienant-core/core/rbac.py

from enum import Enum
from pydantic import BaseModel, Field
from typing import List

class PermissionMode(str, Enum):
    """
    Control de Acceso Basado en Roles (RBAC) estricto para los Nodos Cognitivos.
    """
    PLAN_ONLY = "plan_only"           # Solo puede generar WBS (Planner)
    ROUTING_ONLY = "routing_only"     # Solo decide a qué nodo ir (Orchestrator)
    EDIT_EXECUTE_RBW = "edit_execute_rbw" # Puede modificar código con Read-Before-Write (Logic)
    READ_ONLY = "read_only"           # Analiza, pero no toca el VFS (Analyst)

class AgentIdentity(BaseModel):
    """Contrato inmutable de la identidad de un nodo."""
    name: str = Field(..., description="Nombre del Nodo de Poder")
    role_description: str = Field(..., description="El System Prompt base")
    permission_mode: PermissionMode
    allowed_tools: List[str] = Field(default_factory=list, description="Herramientas MCP autorizadas")

# Instancias de Poder (Nuestros 4 Nodos Base)
PLANNER_IDENTITY = AgentIdentity(
    name="PlannerAgent",
    role_description="Eres el Estratega. Transformas requerimientos en un WBS inmutable.",
    permission_mode=PermissionMode.PLAN_ONLY,
    allowed_tools=[] # Sin herramientas de ejecución
)

LOGIC_IDENTITY = AgentIdentity(
    name="LogicAgent",
    role_description="Eres el Constructor. Ejecutas los pasos del WBS modificando el código.",
    permission_mode=PermissionMode.EDIT_EXECUTE_RBW,
    allowed_tools=["edit_file", "run_terminal"]
)

# ... (Orchestrator y Analyst seguirán este mismo patrón)