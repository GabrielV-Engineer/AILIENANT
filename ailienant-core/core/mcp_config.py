"""Serializable projection of the MCP server catalog (.ailienant/config.json).

The SQLite ``mcp_servers`` table is the runtime source of truth; this module
adds a portable, git-committable JSON projection of it.

  * **Export** redacts any credential embedded in a server uri and reduces a
    regulated server's secret to a ``key_ref`` placeholder — a secret value
    never reaches the JSON.
  * **Import** reconciles a projection back into the catalog with an idempotent
    upsert keyed by server name (case-insensitive), so re-importing the same
    config never duplicates a server.

The actual storage and connect-time injection of a secret value is out of
scope here; ``import`` only signals which servers still need a credential.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Dict, List, Mapping

import core.db as catalog_db
from core.mcp_registry import REGULATED_SERVERS

# Bump only on a breaking change to the projection shape. A payload declaring a
# higher version than this build understands is rejected fail-fast.
MCP_CONFIG_VERSION = 1

# Lowercased names of regulated servers that require a secret. Used to emit a
# key_ref placeholder on export and to flag a pending credential on import.
_SERVERS_WITH_SECRETS = frozenset(
    server.name.lower() for server in REGULATED_SERVERS if server.secrets
)

# Strip the userinfo (user:password) from any embedded credential URL so a
# connection string typed directly into a server uri never lands in the export.
_CREDENTIAL_URL = re.compile(r"(\w+://)[^/?#@\s]+@")


class McpConfigError(ValueError):
    """Raised when an import payload is malformed or declares an unsupported version."""


def _redact_uri_credentials(uri: str) -> str:
    """Replace embedded URL credentials with a placeholder, leaving structure intact."""
    return _CREDENTIAL_URL.sub(r"\1<redacted>@", uri)


def _key_ref_for(name: str) -> str | None:
    """Return the secret placeholder for a regulated server, or None."""
    if name.lower() in _SERVERS_WITH_SECRETS:
        return f"vscode_secret:{name.lower()}"
    return None


async def export_mcp_config() -> Dict[str, Any]:
    """Project the catalog into a portable, credential-free config dict."""
    servers: List[Dict[str, Any]] = []
    for row in await catalog_db.list_mcp_servers():
        name = row["name"]
        entry: Dict[str, Any] = {
            "name": name,
            "transport": row["transport"],
            "uri": _redact_uri_credentials(row["uri"]),
            "enabled": bool(row["enabled"]),
        }
        key_ref = _key_ref_for(name)
        if key_ref is not None:
            entry["key_ref"] = key_ref
        servers.append(entry)
    return {"version": MCP_CONFIG_VERSION, "servers": servers}


async def import_mcp_config(
    payload: Mapping[str, Any],
    *,
    validate_uri: Callable[[str], None],
) -> Dict[str, Any]:
    """Reconcile a config projection into the catalog by server name.

    ``validate_uri(uri)`` receives a server's full uri and must raise
    ``ValueError`` when the launch command is not allowlisted; a rejected
    server is skipped (not imported) without aborting the rest of the batch.

    ``needs_secret`` carries names whose ``key_ref`` was recognized but whose
    secret value is not yet stored — the server IS imported successfully; only
    the credential is pending. It is a UX signal for the import surface, never
    an error.
    """
    if not isinstance(payload, Mapping):
        raise McpConfigError("config payload must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise McpConfigError("config version must be an integer")
    if version > MCP_CONFIG_VERSION:
        raise McpConfigError("unsupported config version")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list):
        raise McpConfigError("config 'servers' must be a list")

    existing = {row["name"].lower(): row["id"] for row in await catalog_db.list_mcp_servers()}

    imported: List[str] = []
    updated: List[str] = []
    skipped: List[Dict[str, str]] = []
    needs_secret: List[str] = []

    for raw in raw_servers:
        if not isinstance(raw, Mapping):
            skipped.append({"name": "", "reason": "server entry must be an object"})
            continue
        name = str(raw.get("name", "")).strip()
        uri = str(raw.get("uri", "")).strip()
        if not name or not uri:
            skipped.append({"name": name, "reason": "name and uri are required"})
            continue
        transport = str(raw.get("transport") or "stdio").strip()
        enabled = bool(raw.get("enabled", True))

        if transport == "stdio":
            try:
                validate_uri(uri)
            except ValueError as exc:
                skipped.append({"name": name, "reason": str(exc)})
                continue

        key = name.lower()
        is_update = key in existing
        server_id = existing[key] if is_update else uuid.uuid4().hex
        await catalog_db.upsert_mcp_server(server_id, name, uri, transport, enabled)
        existing[key] = server_id  # later duplicates in the same payload update in place
        (updated if is_update else imported).append(name)

        if raw.get("key_ref") or key in _SERVERS_WITH_SECRETS:
            needs_secret.append(name)

    return {
        "ok": True,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "needs_secret": needs_secret,
        "servers": await catalog_db.list_mcp_servers(),
    }
