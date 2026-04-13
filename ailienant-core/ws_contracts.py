# alienant-core/core/ws_contracts.py

from pydantic import BaseModel, Field
from typing import Literal, Union, Dict

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
# 3. EL CONTRATO MAESTRO O(1)
# =====================================================================

# FastAPI usará este tipo para validar CUALQUIER mensaje entrante.
# Pydantic usará el campo 'event_type' para castearlo a la clase correcta.
WebSocketMessage = Union[
    ClientFileUpdateEvent, 
    ServerCodeProposalEvent, 
    ServerStatusEvent
]