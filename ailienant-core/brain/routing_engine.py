from typing import Dict, Optional, Tuple
from pydantic import BaseModel

# --- 1. Contratos de Datos (Alineados con SCHEMA_EVOLUTION.MD) ---


class LLMProfile(BaseModel):
    model_name: str
    parameters_b: float  # Capacidad cognitiva (ej. 8.0 para Llama-3 8B)
    context_window: int  # Límite de tokens (ej. 8192)
    is_local: bool  # True para modelos locales, False para APIs externas


class EnvironmentProfile(BaseModel):
    vram_gb: float
    models: Dict[
        str, LLMProfile
    ]  # Roles esperados: "LOCAL_SMALL", "LOCAL_BIG", "CLOUD"


# --- 2. El Motor de Decisión Orientado a Objetos ---


class RoutingEngine:
    @staticmethod
    def select_best_agent(
        css_score: float,
        tci_score: float,
        estimated_tokens: int,
        env: EnvironmentProfile,
    ) -> str:
        """
        Matriz 3D O(M): Determina el agente/modelo óptimo basándose en:
        1. CSS (Contexto): ¿Es suficiente la información?
        2. TCI (Complejidad): ¿Es difícil la tarea?
        3. Capacidad: ¿El hardware soporta la carga y la ventana de contexto?
        """

        # 🔴 DIMENSIÓN 1: Filtro de Seguridad (CSS) -> Strict Local Mode
        if css_score < 40.0:
            print(
                "[ALERTA] CSS deficiente. Faltan datos en GraphRAG. Activando Degradación Elegante."
            )
            return "HUMAN_REQUIRED"

        # 🔵 DIMENSIÓN 2: Requerimiento cognitivo (TCI)
        # TCI > 75: Arquitectura compleja -> requiere > 14B params
        # TCI > 40: Lógica media -> requiere > 7B params
        min_params = 14.0 if tci_score > 75.0 else (7.0 if tci_score > 40.0 else 1.5)

        # 🟢 DIMENSIÓN 3: Match de Capacidad de Hardware
        selected_role = "CLOUD"  # Fallback por defecto si los locales fallan

        for role, profile in env.models.items():
            if not profile.is_local:
                continue  # Protegemos la privacidad analizando solo locales primero

            # Filtro A: Riesgo de OOM (Dejamos 20% de margen para la respuesta)
            if estimated_tokens > (profile.context_window * 0.8):
                continue

            # Filtro B: Capacidad Cognitiva Suficiente
            if profile.parameters_b >= min_params:
                # Nos quedamos con el modelo más ligero que pueda hacer el trabajo
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
