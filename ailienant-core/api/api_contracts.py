# alienant-core/core/api_contracts.py

from pydantic import BaseModel, Field
from typing import List, Optional

# =====================================================================
# MODELOS DE CONTEXTO IDE (VFS Ready)
# =====================================================================

class DirtyBuffer(BaseModel):
    """Representa un archivo modificado en el IDE pero no guardado en disco."""
    path: str = Field(..., description="Ruta absoluta del archivo")
    content: str = Field(..., description="Contenido actual en la RAM de VS Code")

class IDEContext(BaseModel):
    """Cápsula de estado del IDE en el momento exacto de la petición."""
    active_file: str = Field(..., description="Archivo que el usuario tiene abierto")
    # Nota: Mantenemos document_version_id como str para compatibilidad con hashes
    document_version_id: str = Field(..., description="ID de versión para OCC") 
    dirty_buffers: List[DirtyBuffer] = Field(default_factory=list)

# =====================================================================
# REQUEST & RESPONSE PAYLOADS
# =====================================================================

class TaskSubmitRequest(BaseModel):
    """Contrato estricto para el endpoint POST /task/submit (Capa 8)."""
    user_input: str = Field(..., min_length=1, description="El prompt del usuario")
    ide_context: IDEContext

class TaskSubmitResponse(BaseModel):
    """Respuesta de acuse de recibo y enrutamiento."""
    task_id: str
    status: str = Field(pattern="^(accepted|rejected|queued)$")
    message: str