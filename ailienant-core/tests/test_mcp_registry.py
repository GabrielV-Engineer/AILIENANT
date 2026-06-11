# ailienant-core/tests/test_mcp_registry.py
#
# Unit tests for the curated regulated-server registry: it is the single
# source of truth for both install metadata and the authoritative per-tool
# privilege tiers that override the fail-closed verb heuristic.
#
# Every tier assertion checks an EXACT tier (never "!= DANGEROUS") so a test
# cannot pass by accidentally falling through to the fail-closed default.

import re

import pytest

from core.mcp_constants import ALLOWED_MCP_COMMANDS
from core.mcp_registry import (
    REGULATED_SERVERS,
    build_privilege_catalog,
    init_registry,
)
from core.permissions import ToolPrivilegeTier, classify_tool_privilege

R = ToolPrivilegeTier.READ_ONLY
W = ToolPrivilegeTier.WRITE
E = ToolPrivilegeTier.EXECUTE
D = ToolPrivilegeTier.DANGEROUS


@pytest.fixture(autouse=True)
def _registered() -> None:
    """Merge the curated overrides into the permission catalog (idempotent)."""
    init_registry()


# ---------------------------------------------------------------------------
# DoD — the four regulated servers resolve to their correct tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,server,expected",
    [
        ("merge_pull_request", "github", D),
        ("create_pull_request", "github", W),
        ("search", "brave-search", R),
        ("run", "docker", E),
        ("query", "postgres", R),
        ("execute", "postgres", E),
    ],
)
def test_regulated_tools_resolve_via_catalog(
    tool: str, server: str, expected: ToolPrivilegeTier
) -> None:
    assert classify_tool_privilege(tool, server_name=server) is expected


def test_catalog_overrides_the_heuristic_blind_spot() -> None:
    # Probative behavioral delta. Bare "query" carries no recognized verb, so
    # the heuristic alone is fail-closed DANGEROUS. With the postgres server the
    # curated catalog must pin it READ_ONLY — proving the override, not the
    # heuristic, supplied the result.
    assert classify_tool_privilege("query") is D
    assert classify_tool_privilege("query", server_name="postgres") is R


# ---------------------------------------------------------------------------
# Single source of truth — the catalog is derived from the registry
# ---------------------------------------------------------------------------


def test_catalog_is_derived_from_registry() -> None:
    catalog = build_privilege_catalog()
    for server in REGULATED_SERVERS:
        for tool_name, tier in server.tool_tiers.items():
            key = f"{server.name}.{tool_name}".lower()
            assert catalog[key] is tier


# ---------------------------------------------------------------------------
# Install-metadata integrity + zero secret leakage
# ---------------------------------------------------------------------------


def test_install_metadata_is_well_formed() -> None:
    seen_names = set()
    for server in REGULATED_SERVERS:
        assert server.name == server.name.lower()
        assert server.name not in seen_names  # unique namespaces
        seen_names.add(server.name)
        assert server.transport == "stdio"
        assert server.command in ALLOWED_MCP_COMMANDS
        assert server.args  # at least the package/entrypoint
        for secret in server.secrets:
            assert re.match(r"^[A-Z_][A-Z0-9_]*$", secret)


def test_no_secret_or_url_leaks_into_args() -> None:
    # A connection string / URL / oversized token must live in `secrets`, never
    # in the structural argument list.
    for server in REGULATED_SERVERS:
        assert not any("://" in arg or len(arg) > 100 for arg in server.args)
