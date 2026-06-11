"""Shared MCP security-policy constants.

These live in the core layer (not the API/transport layer) so that both the
REST surface that validates user-supplied server URIs and the curated server
registry can enforce the same launch-command allowlist without one importing
the other.
"""

from __future__ import annotations

from typing import FrozenSet

# Command-injection defense for stdio:// MCP servers. A server URI maps to an
# arbitrary executable, so only this allowlist of trusted launchers may run.
# There is deliberately no "any file that exists on disk" fallback — that would
# let an arbitrary interpreter through. Full paths are accepted only when their
# basename matches an entry here.
ALLOWED_MCP_COMMANDS: FrozenSet[str] = frozenset(
    {"npx", "npm", "node", "python", "python3", "py", "uv", "uvx", "deno", "docker"}
)
