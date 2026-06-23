"""Phase 5.1 — Permission Engine (3-axis model + RBWE).

See docs/PHASE_5_BLUEPRINT.md §2 for the architectural contract.

Three orthogonal axes:
    1. AgentIdentity.permission_mode  — per-agent (shared/rbac.py, unchanged)
    2. SessionPermissionMode          — per-mission session policy (THIS MODULE)
    3. ToolPrivilegeTier              — per-tool static tier        (THIS MODULE)

evaluate_action() composes all three into a single PermissionDecision via a pure,
O(1), lru_cached pure function — no I/O, no LLM. rbwe_guard() enforces
Read-Before-Write by consulting state["read_files_state"] (existing channel).
"""

from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from shared.rbac import PermissionMode

if TYPE_CHECKING:
    from brain.state import AIlienantGraphState


# =====================================================================
# 1. ENUMS — Session policy + tool tier + decision outcome
# =====================================================================


class SessionPermissionMode(str, Enum):
    """Session-wide HITL policy. Mutated by user toggle or TogglePlanModeTool."""

    # Canonical vocabulary, severity-ordered permissive -> restrictive. Each member
    # names a distinct (mode, tier) -> ALLOW | HITL | DENY posture resolved by the
    # permission matrix.
    FULL_AUTO = "full_auto"      # No HITL for any tier; all tools execute immediately.
    STANDARD = "standard"        # HITL for DANGEROUS only.
    CAUTIOUS = "cautious"        # HITL for EXECUTE + DANGEROUS; READ_ONLY auto-admitted.
    ASK_EXECUTE = "ask_execute"  # Ask before any non-READ_ONLY tool; deny DANGEROUS.
    ASK_ALL = "ask_all"          # Ask before every tool call, including READ_ONLY.
    READ_ONLY = "read_only"      # Only READ_ONLY tier admitted; EXECUTE/DANGEROUS blocked.
    PLAN_ONLY = "plan_only"      # Planning only; no execution.

    # Deprecated legacy aliases, retained so persisted checkpoints keep resolving.
    # _LEGACY_MODE_MIGRATION maps each onto its behavior-faithful canonical member.
    DEFAULT = "default"   # Deprecated -> CAUTIOUS. HITL on every WRITE / EXECUTE / DANGEROUS.
    PLAN = "plan"         # Deprecated -> PLAN_ONLY. Blocks everything that is not READ_ONLY.
    AUTO = "auto"         # Deprecated -> STANDARD. Uninterrupted; DANGEROUS still HITL.


# Behavior-faithful migration of the deprecated legacy modes onto the canonical
# vocabulary. Each target preserves the legacy mode's real gating behavior, so a
# checkpoint resume never silently loosens or tightens an existing session.
_LEGACY_MODE_MIGRATION: Dict[SessionPermissionMode, SessionPermissionMode] = {
    SessionPermissionMode.DEFAULT: SessionPermissionMode.CAUTIOUS,
    SessionPermissionMode.AUTO: SessionPermissionMode.STANDARD,
    SessionPermissionMode.PLAN: SessionPermissionMode.PLAN_ONLY,
}

# Every accepted session-mode wire value (canonical 7 + deprecated legacy aliases),
# lowercased. Callers seeding state from untrusted preference strings validate
# against this set so an unrecognized value is dropped rather than propagated.
_VALID_SESSION_MODE_VALUES: frozenset[str] = frozenset(
    mode.value for mode in SessionPermissionMode
)


class ToolPrivilegeTier(str, Enum):
    """Per-tool privilege classification. Declared at registration time."""

    READ_ONLY = "read_only"  # No side effects on disk, network mutating ops, or processes.
    WRITE = "write"          # Mutates VFS / workspace files.
    EXECUTE = "execute"      # Spawns subprocesses or background tasks.
    DANGEROUS = "dangerous"  # Irreversible. Always HITL.


class PermissionDecision(str, Enum):
    """Outcome of evaluate_action(). Consumed by gates and audit log."""

    ALLOW = "allow"
    HITL = "hitl"
    DENY = "deny"


# Frontend mode selector → session-wide permission policy. The UI offers a
# three-way choice; each maps to one session mode: Auto runs uninterrupted,
# Ask gates every write through HITL, Plan blocks all non-read-only actions.
_FRONTEND_MODE_TO_SESSION = {
    "automatic": SessionPermissionMode.AUTO,
    "ask_before_edits": SessionPermissionMode.DEFAULT,
    "plan_mode": SessionPermissionMode.PLAN,
}


def session_mode_from_frontend(mode: Optional[str]) -> Optional[SessionPermissionMode]:
    """Map a frontend selector string to a session policy; None if unrecognized.

    An unrecognized or absent value returns None so callers fall back to the
    per-session settings-file seed rather than silently forcing a policy.
    """
    return _FRONTEND_MODE_TO_SESSION.get((mode or "").strip().lower())


# =====================================================================
# 1b. Tool privilege classification — fail-closed
# =====================================================================
#
# Tools discovered from an external MCP server arrive with only a name,
# a free-text description, and a JSON input schema — no trustworthy
# privilege annotation. Trusting that input would let a mutating remote
# tool register as READ_ONLY and slip past evaluate_action()/rbwe_guard()
# without ever surfacing an approval prompt. Classification is therefore
# fail-closed: an unrecognized verb resolves to the most-restricted tier
# (DANGEROUS), never the least. Do not relax this default to READ_ONLY
# for convenience — the whole point is that an unknown remote capability
# is treated as hostile until a curated entry says otherwise.

# Severity ordering for "most restrictive wins" comparisons. The integer
# value is meaningless on its own; only the relative order matters.
_TIER_SEVERITY: Dict[ToolPrivilegeTier, int] = {
    ToolPrivilegeTier.READ_ONLY: 0,
    ToolPrivilegeTier.WRITE: 1,
    ToolPrivilegeTier.EXECUTE: 2,
    ToolPrivilegeTier.DANGEROUS: 3,
}

# Verb tokens that signal each tier, matched by whole-token equality (never
# substring) against the tokenized name and description.
_VERB_SETS: Dict[ToolPrivilegeTier, frozenset[str]] = {
    ToolPrivilegeTier.READ_ONLY: frozenset(
        {"get", "list", "read", "search", "fetch", "describe"}
    ),
    ToolPrivilegeTier.WRITE: frozenset(
        {"create", "update", "write", "push", "add", "set"}
    ),
    ToolPrivilegeTier.EXECUTE: frozenset({"exec", "execute", "run", "invoke", "spawn"}),
    ToolPrivilegeTier.DANGEROUS: frozenset(
        {"delete", "drop", "force", "merge", "reset", "purge"}
    ),
}

# Curated, authoritative tier overrides keyed by "<server>.<tool>" or bare
# "<tool>" (both lowercased). Trusted source — may downgrade as well as
# elevate the heuristic. Empty at module load; the curated regulated-server
# registry merges its entries in via register_privilege_overrides() during
# application startup.
_PRIVILEGE_CATALOG: Dict[str, ToolPrivilegeTier] = {}


def register_privilege_overrides(overrides: Mapping[str, ToolPrivilegeTier]) -> None:
    """Merge curated tier overrides into the catalog (keys lowercased).

    Idempotent: re-registering the same overrides is a no-op beyond the dict
    update, so it is safe to call once at startup and again from tests.
    """
    _PRIVILEGE_CATALOG.update({key.lower(): tier for key, tier in overrides.items()})

# Split on camelCase boundaries (lower→Upper, Upper→Upper+lower) and on any
# run of separator characters, so "mergePullRequest" and "merge_pull_request"
# both yield {"merge", "pull", "request"}.
_TOKEN_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[-_.\s]+")


def _tokenize(text: str) -> set[str]:
    """Break an identifier or description into lowercased word tokens."""
    return {tok for tok in _TOKEN_SPLIT.sub("_", text).lower().split("_") if tok}


def classify_tool_privilege(
    tool_name: str,
    description: str = "",
    server_name: Optional[str] = None,
) -> ToolPrivilegeTier:
    """Resolve a tool's privilege tier from untrusted descriptor metadata.

    Precedence, highest authority first:

    1. Curated catalog — an explicit, trusted override for a known tool.
    2. Verb heuristic — the most restrictive tier whose verb set matches a
       token in the name or the description. The description can only raise
       the tier toward DANGEROUS, never lower it below what the name implies.
    3. Fail-closed default — DANGEROUS when no verb matches anywhere.
    """
    lname = tool_name.lower()
    catalog_keys = ([f"{server_name.lower()}.{lname}"] if server_name else []) + [lname]
    for key in catalog_keys:
        catalogued = _PRIVILEGE_CATALOG.get(key)
        if catalogued is not None:
            return catalogued

    name_tokens = _tokenize(tool_name)
    desc_tokens = _tokenize(description)
    matched: List[ToolPrivilegeTier] = []
    for tier, verbs in _VERB_SETS.items():
        if (name_tokens & verbs) or (desc_tokens & verbs):
            matched.append(tier)

    if matched:
        return max(matched, key=_TIER_SEVERITY.__getitem__)

    return ToolPrivilegeTier.DANGEROUS


# =====================================================================
# 2. PermissionDeniedError — surfaced to agent scratchpad by RBWE
# =====================================================================


class PermissionDeniedError(Exception):
    """Raised by rbwe_guard when a Write/Execute tool targets an unread path.

    Caught and surfaced to the agent's scratchpad (NOT a crashed turn) so the
    agent observes the violation and corrects course by calling FileReadTool first.
    """

    def __init__(self, tool_name: str, target_path: str, reason: str) -> None:
        self.tool_name = tool_name
        self.target_path = target_path
        self.reason = reason
        super().__init__(f"{tool_name} → {target_path!r}: {reason}")


# =====================================================================
# 3. evaluate_action — pure O(1) (mode x tier) decision matrix
# =====================================================================
#
# Authoritative mapping of (canonical session mode, tool tier) -> decision.
# The identity floor is applied first in evaluate_action(); this table assumes a
# mutation-capable identity (or a READ_ONLY tier, which clears the floor). Every
# legacy member resolves through _LEGACY_MODE_MIGRATION before lookup, so the
# table only ever needs the seven canonical rows.
_DECISION_MATRIX: Dict[
    SessionPermissionMode, Dict[ToolPrivilegeTier, PermissionDecision]
] = {
    SessionPermissionMode.FULL_AUTO: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.ALLOW,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.ALLOW,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.ALLOW,
    },
    SessionPermissionMode.STANDARD: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.ALLOW,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.ALLOW,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.HITL,
    },
    SessionPermissionMode.CAUTIOUS: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.HITL,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.HITL,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.HITL,
    },
    SessionPermissionMode.ASK_EXECUTE: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.HITL,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.HITL,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.DENY,
    },
    SessionPermissionMode.ASK_ALL: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.HITL,
        ToolPrivilegeTier.WRITE: PermissionDecision.HITL,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.HITL,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.HITL,
    },
    SessionPermissionMode.READ_ONLY: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.DENY,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.DENY,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.DENY,
    },
    SessionPermissionMode.PLAN_ONLY: {
        ToolPrivilegeTier.READ_ONLY: PermissionDecision.ALLOW,
        ToolPrivilegeTier.WRITE: PermissionDecision.DENY,
        ToolPrivilegeTier.EXECUTE: PermissionDecision.DENY,
        ToolPrivilegeTier.DANGEROUS: PermissionDecision.DENY,
    },
}


@lru_cache(maxsize=None)
def evaluate_action(
    session_mode: SessionPermissionMode,
    tool_tier: ToolPrivilegeTier,
    agent_permission: PermissionMode,
) -> PermissionDecision:
    """Compose session mode, tool tier, and agent identity into one decision.

    Resolution order:
      1. Normalize a deprecated legacy mode onto the canonical vocabulary.
      2. Identity floor — an identity not granted EDIT_EXECUTE_RBW (Planner /
         Researcher / routing-only) can never exceed the READ_ONLY tier, in any
         session mode. READ_ONLY tier clears the floor unconditionally so its
         per-mode posture (e.g. ASK_ALL gates reads) is honored by the matrix.
      3. (mode, tier) lookup against the authoritative _DECISION_MATRIX.
    """

    mode = _LEGACY_MODE_MIGRATION.get(session_mode, session_mode)

    if (
        tool_tier is not ToolPrivilegeTier.READ_ONLY
        and agent_permission is not PermissionMode.EDIT_EXECUTE_RBW
    ):
        return PermissionDecision.DENY

    return _DECISION_MATRIX[mode][tool_tier]


def session_mode_from_channel(raw: Optional[str]) -> SessionPermissionMode:
    """Read a channel-stored session mode into the enum.

    The graph channel stores the policy UPPERCASE (``"DEFAULT" | "PLAN" |
    "AUTO"``) while the enum values are lowercase, so a direct construction of
    the uppercase string raises. Lowercase first, and fall back to the safest
    policy (DEFAULT) on any unrecognized value rather than letting a typo
    escalate privileges.
    """
    try:
        return SessionPermissionMode(str(raw or "DEFAULT").lower())
    except ValueError:
        return SessionPermissionMode.DEFAULT


def gate_execute_action(session_mode: SessionPermissionMode) -> PermissionDecision:
    """Single choke point for any execute-tier dispatch under the coder identity.

    Centralizing the ``(EXECUTE, EDIT_EXECUTE_RBW)`` axes means a future
    graph-wired subprocess dispatch has exactly one place to consult — it cannot
    re-derive the matrix args and accidentally weaken them.
    """
    return evaluate_action(
        session_mode, ToolPrivilegeTier.EXECUTE, PermissionMode.EDIT_EXECUTE_RBW
    )


# =====================================================================
# 4. rbwe_guard — Read-Before-Write enforcement
# =====================================================================


_MUTATING_TIERS = frozenset(
    {ToolPrivilegeTier.WRITE, ToolPrivilegeTier.EXECUTE, ToolPrivilegeTier.DANGEROUS}
)


def rbwe_guard(
    tool_name: str,
    tool_tier: ToolPrivilegeTier,
    target_path: Optional[str],
    state: "AIlienantGraphState | Mapping[str, Any]",
) -> None:
    """Raise PermissionDeniedError if a mutating tool targets an unread path.

    READ_ONLY tier bypasses the guard. Mutating tier with target_path=None
    (e.g. SandboxBashTool without a -c file) also bypasses — the guard only
    fires when there is a concrete filesystem target.
    """

    if tool_tier not in _MUTATING_TIERS:
        return
    if target_path is None:
        return

    read_files_state = state.get("read_files_state", {}) if isinstance(state, Mapping) else {}
    if target_path in read_files_state:
        return

    raise PermissionDeniedError(
        tool_name=tool_name,
        target_path=target_path,
        reason=(
            "RBWE violation: target was never read via FileReadTool. "
            "Call FileReadTool first, then retry."
        ),
    )
