# ailienant-core/api/api_contracts.py

from pydantic import BaseModel, Field
from typing import List, Optional

from brain.state import ManualAttachment  # canonical model defined in brain/state.py

# =====================================================================
# IDE CONTEXT MODELS (VFS Ready)
# =====================================================================


class DirtyBuffer(BaseModel):
    """A file modified in the IDE but not yet saved to disk."""

    path: str = Field(..., description="Absolute path to the file")
    content: str = Field(..., description="Current in-memory content held by VS Code")


class IDEContext(BaseModel):
    """Snapshot of the IDE's state at the exact moment of the request."""

    active_file: str = Field(..., description="File the user currently has open")
    # Kept as str so it stays compatible with content hashes as well as
    # monotonic LSP version numbers.
    document_version_id: str = Field(..., description="Document version used for OCC")
    dirty_buffers: List[DirtyBuffer] = Field(default_factory=list)
    # Optional for backward compatibility during rollout.
    project_id: Optional[str] = None
    explicit_mentions: List[str] = Field(default_factory=list)
    attachments: List[ManualAttachment] = Field(default_factory=list)


# =====================================================================
# REQUEST & RESPONSE PAYLOADS
# =====================================================================


class TaskSubmitRequest(BaseModel):
    """Strict contract for the POST /task/submit endpoint."""

    user_input: str = Field(..., min_length=1, description="The user's prompt")
    ide_context: IDEContext


class TaskSubmitResponse(BaseModel):
    """Acknowledgement and routing response."""

    task_id: str
    status: str = Field(pattern="^(accepted|rejected|queued)$")
    message: str


# =====================================================================
# MODEL DISCOVERY
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
