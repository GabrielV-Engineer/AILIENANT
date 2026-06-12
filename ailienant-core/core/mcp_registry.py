"""Curated registry of regulated MCP servers — the single source of truth.

Each entry carries both halves of what the rest of the system needs:

  * **Install metadata** — the launcher command, structural arguments, and the
    names of any secrets a one-click install must collect. Consumed by the
    registry-browsing UX.
  * **Per-tool privilege tiers** — authoritative overrides that correct the
    fail-closed verb heuristic's blind spots for these well-known servers (for
    example a database ``query`` reads as DANGEROUS to the heuristic but is
    genuinely read-only). Flattened into the permission engine's catalog at
    startup via :func:`init_registry`.

Secrets are never stored here as values — only their environment-variable
names. The value is collected at install time and kept in the editor's secret
storage, referenced indirectly. A connection string is a secret, so it is
declared in ``secrets`` and never embedded in ``args``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from core.mcp_constants import ALLOWED_MCP_COMMANDS
from core.permissions import ToolPrivilegeTier, register_privilege_overrides

_R = ToolPrivilegeTier.READ_ONLY
_W = ToolPrivilegeTier.WRITE
_E = ToolPrivilegeTier.EXECUTE
_D = ToolPrivilegeTier.DANGEROUS

# POSIX-portable environment-variable name: a leading letter or underscore
# followed by letters, digits, or underscores.
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class RegulatedServer:
    """A vetted, one-click-installable MCP server with curated tool tiers."""

    name: str  # logical id, lowercased — the key namespace for tool tiers
    display_name: str
    description: str
    source_url: str  # canonical repository, for source review before install
    transport: str  # only "stdio" is supported today
    command: str  # launcher; must be in ALLOWED_MCP_COMMANDS
    args: Tuple[str, ...]  # STRUCTURAL FLAGS ONLY — never a secret or URL
    secrets: Tuple[str, ...]  # environment-variable NAMES, never their values
    tool_tiers: Mapping[str, ToolPrivilegeTier]

    def __post_init__(self) -> None:
        # Fail-loud invariants. Explicit raises, not asserts: asserts are
        # stripped under `python -O`, which would silently disable these
        # security-relevant checks in an optimized deployment.
        if self.name != self.name.lower():
            raise ValueError(f"server name must be lowercase: {self.name!r}")
        if not self.source_url.startswith("https://"):
            raise ValueError(
                f"source_url must be an https:// URL for {self.name!r}: {self.source_url!r}"
            )
        if self.transport != "stdio":
            raise ValueError(f"unsupported transport for {self.name!r}: {self.transport!r}")
        if self.command not in ALLOWED_MCP_COMMANDS:
            raise ValueError(
                f"launch command not allowlisted for {self.name!r}: {self.command!r}"
            )
        for secret in self.secrets:
            if not _ENV_NAME.match(secret):
                raise ValueError(
                    f"secret name is not a POSIX env-var name for {self.name!r}: {secret!r}"
                )
        # A connection string, URL, or oversized token must never ride in args —
        # those are secrets and belong in `secrets` + secret storage.
        for arg in self.args:
            if "://" in arg or len(arg) > 100:
                raise ValueError(
                    f"argument looks like an embedded secret/URL for {self.name!r}: {arg!r}"
                )


REGULATED_SERVERS: Tuple[RegulatedServer, ...] = (
    RegulatedServer(
        name="github",
        display_name="GitHub",
        description="Repository, issue, and pull-request operations on GitHub.",
        source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        secrets=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        tool_tiers={
            "create_pull_request": _W,
            "merge_pull_request": _D,
        },
    ),
    RegulatedServer(
        name="brave-search",
        display_name="Brave Search",
        description="Web and local search via the Brave Search API.",
        source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-brave-search"),
        secrets=("BRAVE_API_KEY",),
        tool_tiers={
            "search": _R,
        },
    ),
    RegulatedServer(
        name="docker",
        display_name="Docker",
        description="Container lifecycle and image management on the local Docker host.",
        source_url="https://github.com/ckreiling/mcp-server-docker",
        transport="stdio",
        command="uvx",
        args=("mcp-server-docker",),
        secrets=(),
        tool_tiers={
            "run": _E,
        },
    ),
    RegulatedServer(
        name="postgres",
        display_name="PostgreSQL",
        description="Read and run statements against a PostgreSQL database.",
        source_url="https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        transport="stdio",
        command="npx",
        # The connection string is a secret (POSTGRES_CONNECTION_STRING), not an
        # argument — it is collected at install time and injected from secret
        # storage, never persisted in this structural arg list.
        args=("-y", "@modelcontextprotocol/server-postgres"),
        secrets=("POSTGRES_CONNECTION_STRING",),
        tool_tiers={
            "query": _R,
            "execute": _E,
        },
    ),
)


def build_privilege_catalog() -> Dict[str, ToolPrivilegeTier]:
    """Flatten every server's tool tiers into ``"<server>.<tool>"`` keys.

    Keys are lowercased so they match the qualified lookup performed by
    ``classify_tool_privilege``.
    """
    catalog: Dict[str, ToolPrivilegeTier] = {}
    for server in REGULATED_SERVERS:
        for tool_name, tier in server.tool_tiers.items():
            catalog[f"{server.name}.{tool_name}".lower()] = tier
    return catalog


def get_regulated_server(name: str) -> Optional[RegulatedServer]:
    """Return the curated server with this logical name, or ``None``."""
    for server in REGULATED_SERVERS:
        if server.name == name:
            return server
    return None


def serialize_registry(installed_names: Set[str]) -> List[Dict[str, Any]]:
    """Project the curated registry into JSON for the browse-and-install UI.

    Exposes install metadata, the source-review link, and the per-tool privilege
    tiers (the conscious-consent surface) — but only secret NAMES, never values
    (the registry never held values to begin with). ``installed`` reflects
    whether a server with this name is already in the runtime catalog.
    """
    out: List[Dict[str, Any]] = []
    for server in REGULATED_SERVERS:
        out.append(
            {
                "name": server.name,
                "display_name": server.display_name,
                "description": server.description,
                "source_url": server.source_url,
                "command": server.command,
                "args": list(server.args),
                "secrets": list(server.secrets),
                "tool_tiers": {
                    tool: tier.name for tool, tier in server.tool_tiers.items()
                },
                "installed": server.name in installed_names,
            }
        )
    return out


def init_registry() -> None:
    """Merge the curated tier overrides into the permission engine's catalog.

    Idempotent — safe to call once during application startup and again from
    test fixtures.
    """
    register_privilege_overrides(build_privilege_catalog())
