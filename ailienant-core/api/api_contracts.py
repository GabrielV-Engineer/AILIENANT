# alienant-core/core/api_contracts.py

from pydantic import BaseModel, Field
from typing import List, Optional

from brain.state import ManualAttachment  # canonical model defined in brain/state.py

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
    # Phase 1.1.0 / 1.1.0.4 — optional for backward compatibility during rollout
    project_id: Optional[str] = None
    explicit_mentions: List[str] = Field(default_factory=list)
    attachments: List[ManualAttachment] = Field(default_factory=list)


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


# =====================================================================
# MODEL DISCOVERY (Phase 1.6.3)
# =====================================================================


class ModelInfo(BaseModel):
    """Single model entry returned by the discovery endpoint."""

    id: str = Field(..., description="Alias used by LiteLLM, e.g. 'ailienant/medium'")
    name: str = Field(..., description="Underlying model name, e.g. 'llama3.1'")
    provider: str = Field(..., description="'ollama' | 'openai' | 'anthropic' | etc.")
    is_local: bool = Field(..., description="True if the model runs on-device")


class ModelsAvailableResponse(BaseModel):
    """Response envelope for GET /api/v1/models/available."""

    models: List[ModelInfo]
    litellm_available: bool = Field(
        ..., description="True when the LiteLLM proxy responded successfully"
    )
