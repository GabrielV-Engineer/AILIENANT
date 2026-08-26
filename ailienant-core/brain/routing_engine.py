from typing import Union


class RoutingEngine:
    """Hardware residency policy for the local BYOM keep_alive hint.

    Previously also carried three separate CSS/TCI decision-matrix
    implementations (``select_best_agent``, ``get_optimal_provider``,
    ``resolve_provider``) — none of them had a production caller. The single
    live implementation is ``core.memory.context_auditor.derive_routing_decision``
    (with ``hardware_reroute`` for hardware-aware degradation and
    ``agents/researcher.py`` for its own Vision Bypass rule), which is what
    every agent now actually consumes to select a model tier. Removed rather
    than left dormant: three unreachable copies of the same decision — able to
    silently drift from the one that runs — is worse than no copies.
    """

    @staticmethod
    def get_keep_alive(model_alias: str) -> Union[int, str]:
        """Ollama keep_alive hint for the given model alias.

        Small/Medium tiers (< 10B params) stay permanently resident in VRAM for
        sub-second response latency. Big tier (> 10B) unloads after 5 min idle to
        free VRAM for the host IDE without penalising bursts of agent activity.
        """
        from shared.config import MODEL_BIG
        return "5m" if model_alias == MODEL_BIG else -1
