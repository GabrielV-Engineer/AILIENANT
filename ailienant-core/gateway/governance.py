"""Permission posture and caller identity for the External Capability Gateway.

The gateway serves its own capabilities over MCP, and it classifies and gates them
through the *same* permission engine it uses as a consumer of external tools — the
engine is never forked. Two distinct postures cooperate, and keeping them separate is
the crux of the design:

* **Invocation axis** — may an external caller invoke a curated gateway verb at all?
  The gateway's verbs are vetted product surface, so READ_ONLY/EXECUTE verbs are
  pre-authorized while any DANGEROUS verb still routes through human approval (which,
  lacking a human, degrades to a deny-report). This axis is *not* the mode a spawned
  task runs in.
* **Internal-task mode** — what posture does a task submitted by ``run_task`` run
  under? A fixed conservative mode, never silent-AUTO, so the spawned agent's own
  mutating actions gate. An external caller can never raise it.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict

from core.permissions import (
    PermissionDecision,
    PermissionMode,
    SessionPermissionMode,
    classify_tool_privilege,
    evaluate_action,
    register_privilege_overrides,
)
from gateway import catalog
from gateway.catalog import Capability

logger = logging.getLogger("GATEWAY_GOVERNANCE")

# Invocation axis only. AUTO pre-authorizes the curated READ_ONLY/EXECUTE product
# verbs (under AUTO the engine ALLOWs EXECUTE) while still routing DANGEROUS to HITL.
# This is NOT the mode a spawned task runs in — see INTERNAL_TASK_MODE.
_INVOCATION_GATE_MODE = SessionPermissionMode.AUTO

# The conservative mode a gateway-submitted task runs under. CAUTIOUS — never
# FULL_AUTO or STANDARD — so the spawned agent's WRITE/EXECUTE/DANGEROUS actions
# gate through HITL (and, with no human in an external caller's loop, degrade to
# deny). External callers cannot raise this posture. Matches the canonical mode
# the "ask_before_edits" frontend key resolves to.
INTERNAL_TASK_MODE = SessionPermissionMode.CAUTIOUS

# Keys a caller might try to pass to escalate the spawned task's posture. All ignored.
_ESCALATION_KEYS = (
    "mode",
    "execution_mode",
    "session_permission_mode",
    "permission_mode",
)


def resolve_caller_id() -> str:
    """Derive a stable per-caller identity (one stdio process = one caller).

    An explicit id wins; otherwise the gateway token is hashed; otherwise the caller
    is anonymous. Distinct external agents (distinct tokens) get distinct ledger rows.
    """
    explicit = os.environ.get("AILIENANT_GATEWAY_CALLER_ID")
    if explicit and explicit.strip():
        return explicit.strip()
    token = os.environ.get("AILIENANT_GATEWAY_TOKEN")
    if token and token.strip():
        return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:16]
    return "anonymous"


def register_gateway_privileges() -> None:
    """Register the gateway's own verbs into the shared privilege catalog (D8).

    Idempotent. Makes ``classify_tool_privilege(verb)`` a deterministic catalog hit
    instead of a heuristic guess, so the gateway's tiers are authoritative.
    """
    register_privilege_overrides({cap.name: cap.tier for cap in catalog.CATALOG})


def authorize_invocation(cap: Capability) -> PermissionDecision:
    """Decide whether an external caller may invoke a gateway verb.

    Routes through the same classify → evaluate engine the consumer side uses, on the
    invocation axis. READ_ONLY/EXECUTE → ALLOW, DANGEROUS → HITL.
    """
    tier = classify_tool_privilege(cap.name)
    return evaluate_action(_INVOCATION_GATE_MODE, tier, PermissionMode.EDIT_EXECUTE_RBW)


def resolve_internal_task_mode(arguments: Dict[str, Any]) -> SessionPermissionMode:
    """Resolve the posture a gateway-submitted task runs under — always conservative.

    Any caller-supplied mode/escalation key is ignored; the posture is fixed. This is
    the no-self-escalation guarantee.
    """
    attempted = [key for key in _ESCALATION_KEYS if key in arguments]
    if attempted:
        logger.debug(
            "Ignoring caller-supplied escalation keys %s; task posture fixed to %s.",
            attempted,
            INTERNAL_TASK_MODE.value,
        )
    return INTERNAL_TASK_MODE


# Register at import so classification is deterministic for any dispatch, independent
# of server-construction order. Idempotent; the gateway process is the only importer,
# so this never perturbs the host's catalog.
register_gateway_privileges()
