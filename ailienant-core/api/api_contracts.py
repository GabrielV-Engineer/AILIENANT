# ailienant-core/api/api_contracts.py

from pydantic import BaseModel, Field
from typing import List

# =====================================================================
# IDE CONTEXT MODELS (VFS Ready)
# =====================================================================


class DirtyBuffer(BaseModel):
    """A file modified in the IDE but not yet saved to disk."""

    path: str = Field(..., description="Absolute path to the file")
    content: str = Field(..., description="Current in-memory content held by VS Code")


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
