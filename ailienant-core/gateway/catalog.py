"""Capability catalog for the External Capability Gateway.

This module is the single source of truth for the verbs the gateway exposes over
MCP. Each capability declares its name, a human description, a privilege tier, a
JSON-Schema for its arguments, whether it runs asynchronously, and a per-capability
schema version.

The catalog is declarative only. The privilege tier here is metadata that the
permission gate consumes downstream; the schema version backs the surface's
deprecation policy. The handlers that make these verbs actually respond are wired
separately — this module never executes anything.

Async verbs follow a poll-pair contract: a long-running verb returns a task handle
immediately and the caller polls ``check_task_status`` until completion. This suits
the stdio JSON-RPC transport, which has no streaming back-channel an external model
can consume natively.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import mcp.types as types

from core.permissions import ToolPrivilegeTier

# Bumped when a capability's argument schema changes in a breaking way.
SCHEMA_VERSION = "1.0.0"

# Semver of the gateway surface as a permanent public contract. The single source
# of truth for the version advertised in list_tools() and the MCP initialize
# handshake. A breaking change to the surface bumps the major and announces
# deprecation on the affected capability; callers pin a version.
PROTOCOL_VERSION = "1.0.0"

_TASK_ID_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Handle returned by run_task or run_benchmark.",
        }
    },
    "required": ["task_id"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Capability:
    """One verb exposed by the gateway."""

    name: str
    description: str
    tier: ToolPrivilegeTier
    input_schema: Dict[str, Any]
    is_async: bool
    schema_version: str = SCHEMA_VERSION
    # Deprecation metadata. A breaking schema change marks the old capability
    # deprecated and pins the release it disappears in; callers read this off
    # list_tools() and migrate before the sunset.
    deprecated: bool = False
    deprecated_since: Optional[str] = None
    sunset_version: Optional[str] = None


CATALOG: Tuple[Capability, ...] = (
    Capability(
        name="run_task",
        description=(
            "Submit a coding task to AILIENANT. Returns a task_id immediately; "
            "poll check_task_status with that id until the task completes."
        ),
        tier=ToolPrivilegeTier.EXECUTE,
        is_async=True,
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task instruction for the agent.",
                },
                "workspace_root": {
                    "type": "string",
                    "description": "Absolute path of the target workspace.",
                },
            },
            "required": ["prompt", "workspace_root"],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="run_benchmark",
        description=(
            "Run the AILIENANT benchmark harness. Returns a task_id immediately; "
            "poll check_task_status, then read get_report when it completes."
        ),
        tier=ToolPrivilegeTier.EXECUTE,
        is_async=True,
        input_schema={
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "description": "Optional benchmark suite identifier.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="check_task_status",
        description=(
            "Poll the status and result of a previously submitted task or benchmark."
        ),
        tier=ToolPrivilegeTier.READ_ONLY,
        is_async=False,
        input_schema=_TASK_ID_SCHEMA,
    ),
    Capability(
        name="query_memory",
        description="Query AILIENANT's GraphRAG memory for relevant snippets.",
        tier=ToolPrivilegeTier.READ_ONLY,
        is_async=False,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "workspace_root": {
                    "type": "string",
                    "description": "Absolute path of the target workspace.",
                },
            },
            "required": ["query", "workspace_root"],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="get_dependents",
        description="Return the symbols that depend on a given symbol (1-hop backward).",
        tier=ToolPrivilegeTier.READ_ONLY,
        is_async=False,
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Fully-qualified symbol."},
                "workspace_root": {
                    "type": "string",
                    "description": "Absolute path of the target workspace.",
                },
            },
            "required": ["symbol", "workspace_root"],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="get_workspace_graph",
        description="Return a snapshot of the workspace code-dependency graph.",
        tier=ToolPrivilegeTier.READ_ONLY,
        is_async=False,
        input_schema={
            "type": "object",
            "properties": {
                "workspace_root": {
                    "type": "string",
                    "description": "Absolute path of the target workspace.",
                }
            },
            "required": ["workspace_root"],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="get_report",
        description="Read the machine-readable report produced by a benchmark run.",
        tier=ToolPrivilegeTier.READ_ONLY,
        is_async=False,
        input_schema=_TASK_ID_SCHEMA,
    ),
)


def get_capability(name: str) -> Capability | None:
    """Look up a capability by name, or ``None`` if it is not in the catalog."""
    for cap in CATALOG:
        if cap.name == name:
            return cap
    return None


def to_mcp_tools() -> list[types.Tool]:
    """Project the catalog into MCP ``Tool`` descriptors for ``list_tools``.

    The surface version, per-capability schema version, and privilege tier are
    surfaced in each tool's metadata so a caller can discover the contract — and
    any deprecation — without an out-of-band channel.
    """
    return [
        types.Tool(
            name=cap.name,
            description=cap.description,
            inputSchema=cap.input_schema,
            annotations=types.ToolAnnotations(
                readOnlyHint=cap.tier == ToolPrivilegeTier.READ_ONLY,
            ),
            _meta=_tool_meta(cap),
        )
        for cap in CATALOG
    ]


def _tool_meta(cap: Capability) -> Dict[str, Any]:
    """Build a tool's wire metadata, omitting empty deprecation fields.

    The deprecation detail keys appear only on a capability that is actually
    deprecated, so the common (non-deprecated) tool carries no null noise.
    """
    meta: Dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": cap.schema_version,
        "tier": cap.tier.value,
        "async": cap.is_async,
        "deprecated": cap.deprecated,
    }
    if cap.deprecated:
        if cap.deprecated_since is not None:
            meta["deprecated_since"] = cap.deprecated_since
        if cap.sunset_version is not None:
            meta["sunset_version"] = cap.sunset_version
    return meta
