# ailienant-core/agents/contract_guard.py
"""Phase 2.23 — ContractGuardNode (Event-Driven Context Anchoring).

Deterministic O(1) middleware sitting between CoderAgent and FinOpsGate. It watches
three drift signals after every CoderAgent turn and short-circuits as pass-through
when none fires. Only on trigger does it call the LLM (structured output) to mint a
SessionContract and emit a render payload for the VS Code persistent banner.

The node also owns the contract_anchor snapshot, so trigger evaluation and side
effect live behind a single ownership boundary (see plan §Step 3 rationale).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("AILIENANT_CONTRACT_GUARD")

TCI_DELTA_THRESHOLD: float = 15.0
CSS_RED_ALERT_THRESHOLD: float = 40.0
TOKEN_CAPACITY_RATIO: float = 0.80

TriggerReason = Literal["TCI_DELTA", "CSS_AT_CAPACITY", "SUBGRAPH_SHIFT", "NONE"]


class SessionContract(BaseModel):
    """Persistent contract surfaced to the user when context drift is detected."""

    mission_outcome: str = Field(
        description="One-sentence restatement of mission_spec.outcome."
    )
    active_role: str = Field(description="Current target_role driving the agent.")
    in_scope: List[str] = Field(
        default_factory=list, description="Top files / modules currently in scope."
    )
    out_of_scope: List[str] = Field(
        default_factory=list,
        description="Explicit out-of-scope items the agent must not touch.",
    )
    open_constraints: List[str] = Field(
        default_factory=list, description="Live constraints that still bind the agent."
    )
    trigger_reason: TriggerReason = Field(description="Which guard signal fired.")


# Type alias for the injectable LLM call. Returns the raw JSON string the model
# produced (matches the project pattern of model_validate_json post-hoc).
LLMInvoker = Callable[[str, Dict[str, Any]], Awaitable[str]]


class ContractGuardNode:
    """Async callable LangGraph node. Pure pass-through unless a trigger fires."""

    def __init__(self, llm_invoker: Optional[LLMInvoker] = None) -> None:
        self._llm_invoker: Optional[LLMInvoker] = llm_invoker

    @staticmethod
    def _evaluate_triggers(state: Dict[str, Any]) -> TriggerReason:
        """Deterministic O(1) check across the three drift signals."""
        anchor: Optional[Dict[str, Any]] = state.get("contract_anchor")
        tci: float = float(state.get("tci", 0.0))
        css: float = float(state.get("css", 100.0))
        target_role: Optional[str] = state.get("target_role")

        token_usage = state.get("token_usage")
        llm_profile = state.get("active_llm_profile")
        if token_usage is not None and llm_profile is not None:
            consumed = float(getattr(token_usage, "local", 0) or 0) + float(
                getattr(token_usage, "cloud", 0) or 0
            )
            window = float(getattr(llm_profile, "context_window", 0) or 0)
            if (
                window > 0
                and css < CSS_RED_ALERT_THRESHOLD
                and (consumed / window) >= TOKEN_CAPACITY_RATIO
            ):
                return "CSS_AT_CAPACITY"

        if anchor is None:
            return "NONE"

        prev_tci = float(anchor.get("tci", tci))
        if abs(tci - prev_tci) > TCI_DELTA_THRESHOLD:
            return "TCI_DELTA"

        prev_role = anchor.get("target_role")
        if target_role is not None and prev_role != target_role:
            return "SUBGRAPH_SHIFT"

        return "NONE"

    async def _default_llm_invoker(self, system: str, user_payload: Dict[str, Any]) -> str:
        """Default LLM call using the project's LLMGateway. Deferred import avoids
        circular dependencies at module load (same idiom as drift_monitor/finops)."""
        import json

        from tools.llm_gateway import LLMGateway, MODEL_MEDIUM

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ]
        response = await LLMGateway.ainvoke(
            messages=messages,
            model=MODEL_MEDIUM,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return str(response.choices[0].message.content or "")

    async def _mint_contract(
        self, state: Dict[str, Any], reason: TriggerReason
    ) -> SessionContract:
        """Invoke structured-output LLM. Falls back to a deterministic skeleton on
        any failure (network, parse, validation) so DoD stays green even offline."""
        mission = state.get("mission_spec")
        active_role: str = state.get("target_role") or "Refactor"
        mission_outcome: str = getattr(mission, "outcome", "") if mission else ""
        scope: List[str] = list(getattr(mission, "scope", []) if mission else [])
        constraints: List[str] = list(
            getattr(mission, "constraints", []) if mission else []
        )

        invoker: LLMInvoker = self._llm_invoker or self._default_llm_invoker
        try:
            raw_json = await invoker(
                "You mint a concise SessionContract that re-anchors a drifting agent "
                "session. Respond ONLY with JSON matching the SessionContract schema: "
                "{mission_outcome, active_role, in_scope, out_of_scope, "
                "open_constraints, trigger_reason}.",
                {
                    "mission_outcome": mission_outcome,
                    "scope": scope,
                    "constraints": constraints,
                    "active_role": active_role,
                    "trigger_reason": reason,
                },
            )
            return SessionContract.model_validate_json(raw_json)
        except (ValidationError, ValueError, KeyError, AttributeError, RuntimeError) as exc:
            logger.warning(
                "Contract minting failed (%s); falling back to deterministic skeleton.",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — network/LiteLLM errors must not break graph
            logger.warning(
                "Contract minting failed (%s); falling back to deterministic skeleton.",
                exc,
            )

        return SessionContract(
            mission_outcome=mission_outcome or "(unset)",
            active_role=active_role,
            in_scope=scope,
            out_of_scope=[],
            open_constraints=constraints,
            trigger_reason=reason,
        )

    async def __call__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        reason: TriggerReason = self._evaluate_triggers(state)
        if reason == "NONE":
            return {}

        logger.info(
            "ContractGuard fired: reason=%s tci=%.1f css=%.1f role=%s",
            reason,
            float(state.get("tci", 0.0)),
            float(state.get("css", 0.0)),
            state.get("target_role"),
        )

        contract = await self._mint_contract(state, reason)
        prev_anchor: Dict[str, Any] = state.get("contract_anchor") or {}
        prev_turn: int = int(prev_anchor.get("turn", 0) or 0)

        return {
            "ui_payload": {
                "action": "RENDER_PERSISTENT_CONTRACT",
                "contract": contract.model_dump(),
                "reason": reason,
            },
            "contract_anchor": {
                "tci": float(state.get("tci", 0.0)),
                "target_role": state.get("target_role"),
                "turn": prev_turn + 1,
            },
        }


_default_node = ContractGuardNode()


async def run_contract_guard_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Async functional wrapper for LangGraph node registration. Mirrors the
    run_*_node convention used by every other agent in this package."""
    return await _default_node(state)
