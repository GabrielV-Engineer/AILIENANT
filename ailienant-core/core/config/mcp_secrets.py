"""Backend-masked secret store for MCP server credentials.

Each regulated server declares the NAMES of the environment variables it needs
(a personal access token, an API key, a connection string). The values are
collected at install time and persisted here — never in the server's ``uri`` and
never in the portable ``config.json`` projection. At connect time the launcher
injects them as process environment for the stdio child.

Storage mirrors the established BYOM credential substrate: a JSON file co-located
with the catalog DB, written atomically with ``0600`` permissions, and masked on
read so a GET response never echoes a real secret back to the client.

Platform note: ``os.chmod(0600)`` only toggles the read-only bit on Windows — it
does not restrict the file to its owner. The protection there is the same as
BYOM's: the file lives under the user-profile-protected catalog directory
(``C:\\Users\\<user>\\...``). This is owner-only enforcement on POSIX and
profile-scoped on Windows, not a cross-platform owner-only ACL.

Concurrency: writes are atomic (``mkstemp`` + ``os.replace``), but the
read-modify-write of an individual update is last-writer-wins under concurrent
callers. This matches BYOM and is acceptable for the single-user dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import stat
import tempfile
from typing import Dict

from shared.config import DB_CATALOG_PATH

logger = logging.getLogger("MCP_SECRETS")

# Co-locate with the catalog DB so the path is deterministic regardless of the
# process working directory — identical resolution to byom_config.
_CATALOG_PATH: pathlib.Path = pathlib.Path(DB_CATALOG_PATH).resolve()
MCP_SECRETS_PATH: pathlib.Path = _CATALOG_PATH.parent / "mcp_secrets.json"

# A generic redaction marker. Deliberately NOT the BYOM "sk-" prefix: these
# values are tokens and connection strings, not OpenAI-style keys.
_MASK_PREFIX = "••••"


def _mask_value(value: str) -> str:
    """Return a masked secret safe for GET responses. Empty → empty string."""
    if not value:
        return ""
    suffix = value[-4:] if len(value) >= 8 else ""
    return _MASK_PREFIX + suffix


def is_masked(value: str) -> bool:
    """True when the value is a round-tripped masked secret, not a real one.

    A masked value re-submitted from the UI must never overwrite the stored
    secret — callers use this to skip such fields on update.
    """
    return value.startswith(_MASK_PREFIX)


def load_mcp_secrets() -> Dict[str, Dict[str, str]]:
    """Read the whole store. Returns ``{}`` on a missing or corrupt file."""
    if not MCP_SECRETS_PATH.exists():
        return {}
    try:
        data = json.loads(MCP_SECRETS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a corrupt store must never crash a read
        logger.warning("Invalid %s — treating as empty: %s", MCP_SECRETS_PATH, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to the declared shape; drop anything malformed rather than trusting it.
    result: Dict[str, Dict[str, str]] = {}
    for server_name, env in data.items():
        if isinstance(env, dict):
            result[str(server_name)] = {
                str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))
            }
    return result


def _save_all(store: Dict[str, Dict[str, str]]) -> None:
    """Atomic + 0600 + UTF-8 write of the whole store."""
    MCP_SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(store, indent=2)
    fd, tmp = tempfile.mkstemp(dir=MCP_SECRETS_PATH.parent, prefix=".tmp_mcpsec_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.replace(tmp, MCP_SECRETS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_server_secrets(server_name: str, secrets: Dict[str, str]) -> None:
    """Merge ``secrets`` into the named server's entry.

    A value that is already masked (a round-tripped placeholder from the UI) is
    skipped so it never clobbers the real stored secret. Empty values are kept
    out of the store entirely.
    """
    store = load_mcp_secrets()
    entry = dict(store.get(server_name, {}))
    for key, value in secrets.items():
        if not value or is_masked(value):
            continue
        entry[key] = value
    if entry:
        store[server_name] = entry
        _save_all(store)


def get_server_env(server_name: str) -> Dict[str, str]:
    """Return the RAW secret values for a server — for env injection only.

    Never serialize this into an HTTP response; use :func:`mask_server_secrets`.
    """
    return dict(load_mcp_secrets().get(server_name, {}))


def mask_server_secrets(server_name: str) -> Dict[str, str]:
    """Return a masked projection of a server's secrets, safe for GET responses."""
    return {k: _mask_value(v) for k, v in load_mcp_secrets().get(server_name, {}).items()}


def delete_server_secrets(server_name: str) -> None:
    """Remove a server's stored secrets. No-op when absent."""
    store = load_mcp_secrets()
    if server_name in store:
        del store[server_name]
        _save_all(store)
