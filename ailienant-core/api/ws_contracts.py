# alienant-core/core/ws_contracts.py

from pydantic import BaseModel, Field
from typing import Literal, Optional, Union

# =====================================================================
# 1. PAYLOADS DE LOS EVENTOS
# =====================================================================


class FileUpdatePayload(BaseModel):
    """Payload para cuando el IDE envía código fresco al Backend."""

    filepath: str = Field(..., description="Ruta absoluta del archivo en el IDE")
    content: str = Field(..., description="Contenido actual en el buffer de VS Code")
    # Este es el corazón del OCC que definimos en state.py
    document_version_id: str = Field(..., description="Timestamp o Hash del IDE")


class CodeProposalPayload(BaseModel):
    """Payload para cuando la IA quiere escribir en el IDE."""

    filepath: str
    proposed_content: str
    # La IA envía el ID sobre el cual basó sus cálculos
    base_document_version_id: str
    agent_signature: str = Field(..., description="Agente que propone (ej. LogicAgent)")


class StatusPayload(BaseModel):
    """Payload efímero para actualizar la UI de la extensión."""

    agent_name: str
    status_message: str
    is_error: bool = False


# =====================================================================
# 2. ENVOLTORIOS DE EVENTOS (Tagged Unions)
# =====================================================================


class ClientFileUpdateEvent(BaseModel):
    # La clave 'event_type' es nuestro discriminador constante
    event_type: Literal["client_file_update"] = "client_file_update"
    data: FileUpdatePayload


class ServerCodeProposalEvent(BaseModel):
    event_type: Literal["server_code_proposal"] = "server_code_proposal"
    data: CodeProposalPayload


class ServerStatusEvent(BaseModel):
    event_type: Literal["server_status"] = "server_status"
    data: StatusPayload


# =====================================================================
# 3. PHASE 1.4 — BIDIRECTIONAL STREAMING & HITL CONTRACTS
# =====================================================================


class TokenChunkPayload(BaseModel):
    """Single LLM output token streamed to the IDE."""

    token: str
    step_id: Optional[int] = None   # WBS step currently executing, if known


class TelemetryPayload(BaseModel):
    """Routing telemetry snapshot for the IDE status bar."""

    session_id: str
    routing_decision: str           # "LOCAL_SMALL" | "LOCAL_BIG" | "CLOUD"
    css_total: float
    task_complexity_index: float
    is_red_alert: bool


class GraphMutationPayload(BaseModel):
    """WBS step status transition emitted by the Orchestrator."""

    step_number: int
    new_status: Literal["pending", "in_progress", "completed", "failed"]
    agent_name: Optional[str] = None


class PlannerModeTogglePayload(BaseModel):
    """Client request to switch Planner-only mode on or off."""

    active: bool


class HITLApprovalRequestPayload(BaseModel):
    """Backend suspends and asks the human to approve a proposed action."""

    session_id: str
    approval_id: str                    # UUID4 — unique per request; client must echo this back
    action_description: str
    proposed_content: Optional[str] = None


class HITLResponsePayload(BaseModel):
    """Client response to a pending HITL approval request."""

    approval_id: str                    # Must match the approval_id from the request
    approved: bool
    comment: Optional[str] = None


# --- Server → Client Events ---

class ServerTokenChunkEvent(BaseModel):
    event_type: Literal["server_token_chunk"] = "server_token_chunk"
    data: TokenChunkPayload


class ServerTelemetryEvent(BaseModel):
    event_type: Literal["server_telemetry"] = "server_telemetry"
    data: TelemetryPayload


class ServerGraphMutationEvent(BaseModel):
    event_type: Literal["server_graph_mutation"] = "server_graph_mutation"
    data: GraphMutationPayload


class ServerHITLApprovalRequestEvent(BaseModel):
    event_type: Literal["server_hitl_approval_request"] = "server_hitl_approval_request"
    data: HITLApprovalRequestPayload


# --- Client → Server Events ---

class ClientPlannerModeToggleEvent(BaseModel):
    event_type: Literal["client_planner_mode_toggle"] = "client_planner_mode_toggle"
    data: PlannerModeTogglePayload


class ClientHITLResponseEvent(BaseModel):
    event_type: Literal["client_hitl_response"] = "client_hitl_response"
    data: HITLResponsePayload


# =====================================================================
# 5. OCC — OPTIMISTIC CONCURRENCY CONTROL (Phase 1.5)
# =====================================================================


class ConcurrencyConflictPayload(BaseModel):
    """Client reports a file version conflict detected during inference."""

    filepath: str = Field(..., description="Absolute path of the conflicting file")
    expected_version: int = Field(..., description="Version at task submission")
    actual_version: int = Field(..., description="Current version after user edited the file")


class ClientConcurrencyConflictEvent(BaseModel):
    event_type: Literal["client_concurrency_conflict"] = "client_concurrency_conflict"
    data: ConcurrencyConflictPayload


# =====================================================================
# 6. PHASE 2.2 — MODEL WARMUP SIGNAL
# =====================================================================


class ModelWarmupPayload(BaseModel):
    """Emitted before a local model inference call that may require warmup time."""

    model_name: str
    is_local: bool
    tier: Literal["small", "medium", "big"]


class ServerModelWarmupEvent(BaseModel):
    event_type: Literal["server_model_warmup"] = "server_model_warmup"
    data: ModelWarmupPayload


# =====================================================================
# 7. PHASE 2.5 — LAZY WORKSPACE INDEXING CONTRACTS
# =====================================================================


class WorkspaceInitPayload(BaseModel):
    """Client announces the workspace root path for lazy background indexing."""
    workspace_root: str
    project_id: str


class IndexingProgressPayload(BaseModel):
    """Server progress broadcast during lazy workspace indexing."""
    current: int
    total: int
    percentage: float


class ClientWorkspaceInitEvent(BaseModel):
    event_type: Literal["client_workspace_init"] = "client_workspace_init"
    data: WorkspaceInitPayload


class ServerIndexingProgressEvent(BaseModel):
    event_type: Literal["server_indexing_progress"] = "server_indexing_progress"
    data: IndexingProgressPayload


# =====================================================================
# 8. PHASE 2.1.13 — FILE DELETE / UNLINK EVENTS
# =====================================================================


class FileDeletePayload(BaseModel):
    """Payload for IDE file deletion (unlink) events."""
    filepath: str = Field(..., description="Absolute path of the deleted file")
    project_id: str = Field(default="", description="Project scope for DB purge")


class ClientFileDeleteEvent(BaseModel):
    event_type: Literal["client_file_delete"] = "client_file_delete"
    data: FileDeletePayload


# =====================================================================
# 4. EL CONTRATO MAESTRO O(1)
# =====================================================================

# FastAPI usará este tipo para validar CUALQUIER mensaje entrante.
# Pydantic usará el campo 'event_type' para castearlo a la clase correcta.

WebSocketMessage = Union[
    ClientFileUpdateEvent,
    ServerCodeProposalEvent,
    ServerStatusEvent,
    ServerTokenChunkEvent,
    ServerTelemetryEvent,
    ServerGraphMutationEvent,
    ServerHITLApprovalRequestEvent,
    ClientPlannerModeToggleEvent,
    ClientHITLResponseEvent,
    ClientConcurrencyConflictEvent,
    ServerModelWarmupEvent,
    ClientWorkspaceInitEvent,        # Phase 2.5
    ServerIndexingProgressEvent,     # Phase 2.5
    ClientFileDeleteEvent,           # Phase 2.1.13
]
