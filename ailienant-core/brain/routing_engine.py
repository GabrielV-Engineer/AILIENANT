import logging
from typing import Dict, Optional, Tuple, Union
from pydantic import BaseModel

logger = logging.getLogger("ROUTING_ENGINE")

# --- 1. Data contracts (aligned with SCHEMA_EVOLUTION.MD) ---


class LLMProfile(BaseModel):
    model_name: str
    parameters_b: float  # Cognitive capacity (e.g. 8.0 for Llama-3 8B)
    context_window: int  # Token limit (e.g. 8192)
    is_local: bool  # True for local models, False for external APIs


class EnvironmentProfile(BaseModel):
    vram_gb: float
    models: Dict[
        str, LLMProfile
    ]  # Expected roles: "LOCAL_SMALL", "LOCAL_BIG", "CLOUD"


# --- 2. The object-oriented decision engine ---


class RoutingEngine:
    @staticmethod
    def select_best_agent(
        css_score: float,
        tci_score: float,
        estimated_tokens: int,
        env: EnvironmentProfile,
    ) -> str:
        """
        3D matrix O(M): selects the optimal agent/model based on:
        1. CSS (context): is the available information sufficient?
        2. TCI (complexity): how hard is the task?
        3. Capacity: can the hardware carry the load and the context window?
        """

        # Dimension 1: safety filter (CSS) -> strict local mode
        if css_score < 40.0:
            logger.warning(
                "Insufficient CSS — missing GraphRAG context; engaging graceful degradation."
            )
            return "HUMAN_REQUIRED"

        # Dimension 2: cognitive requirement (TCI)
        # TCI > 75: complex architecture -> requires > 14B params
        # TCI > 40: medium logic -> requires > 7B params
        min_params = 14.0 if tci_score > 75.0 else (7.0 if tci_score > 40.0 else 1.5)

        # Dimension 3: hardware-capacity match
        selected_role = "CLOUD"  # default fallback when no local model qualifies

        for role, profile in env.models.items():
            if not profile.is_local:
                continue  # privacy-first: evaluate local models before any cloud target

            # Filter A: OOM risk (reserve 20% headroom for the response)
            if estimated_tokens > (profile.context_window * 0.8):
                continue

            # Filter B: sufficient cognitive capacity
            if profile.parameters_b >= min_params:
                # Keep the lightest model that can still do the job
                if (
                    selected_role == "CLOUD"
                    or profile.parameters_b < env.models[selected_role].parameters_b
                ):
                    selected_role = role

        return selected_role

    @staticmethod
    def get_optimal_provider(tci: float, css: float) -> str:
        """
        Simplified 2D routing matrix for the orchestrator node.

        Returns "LOCAL" | "CLOUD" | "HUMAN_REQUIRED".
        Priority order:
          1. CSS < 40  → HUMAN_REQUIRED (context gap triggers graceful degradation)
          2. TCI < 30  → LOCAL (simple task, privacy-first)
          3. TCI >= 30 → CLOUD (cognitive demand exceeds local tier)
        """
        if css < 40.0:
            return "HUMAN_REQUIRED"
        if tci < 30.0:
            return "LOCAL"
        return "CLOUD"

    @staticmethod
    def resolve_provider(
        tci: float,
        css: float,
        has_images: bool = False,
        cloud_available: bool = True,
    ) -> Tuple[str, Optional[str]]:
        """Full 3D decision matrix with Vision Bypass and Cloud Guard fallback.

        Returns (provider, routing_warning | None).

        Priority order (first matching rule wins):
          1. CSS < 40              → HUMAN_REQUIRED  (context gap overrides all)
          2. has_images + cloud    → CLOUD            (vision bypass — multimodal)
          3. has_images + no cloud → HUMAN_REQUIRED   (cannot process images locally)
          4. TCI < 30              → LOCAL            (simple task, privacy-first)
          5. TCI >= 30 + cloud     → CLOUD
          6. TCI >= 30 + no cloud  → LOCAL + warning  (graceful degradation)
        """
        # Rule 1: Context gap — not enough information to proceed safely
        if css < 40.0:
            return "HUMAN_REQUIRED", None

        # Rules 2 & 3: Vision Bypass
        if has_images:
            if cloud_available:
                return "CLOUD", None
            return "HUMAN_REQUIRED", None

        # Rule 4: Low-complexity task → privacy-first local
        if tci < 30.0:
            return "LOCAL", None

        # Rules 5 & 6: High-complexity task — prefer CLOUD, degrade to LOCAL
        if cloud_available:
            return "CLOUD", None

        warning = (
            f"CLOUD optimal for TCI={tci:.1f} but no cloud provider available. "
            "Falling back to LOCAL — response quality may be reduced."
        )
        return "LOCAL", warning

    @staticmethod
    def get_keep_alive(model_alias: str) -> Union[int, str]:
        """Ollama keep_alive hint for the given model alias.

        Small/Medium tiers (< 10B params) stay permanently resident in VRAM for
        sub-second response latency. Big tier (> 10B) unloads after 5 min idle to
        free VRAM for the host IDE without penalising bursts of agent activity.
        """
        from shared.config import MODEL_BIG
        return "5m" if model_alias == MODEL_BIG else -1
