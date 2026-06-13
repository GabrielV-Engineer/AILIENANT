# ailienant-core/tests/test_gateway_dod.py
#
# Division 8.5 DoD-check for the External Capability Gateway: an external caller
# lists the catalog, runs a READ_ONLY verb, and is denied + reported on a
# DANGEROUS verb without hanging. Hermetic — the ledger is isolated and the
# memory backend is stubbed; no real host, socket, or model.

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Iterator

import pytest

from core.permissions import ToolPrivilegeTier, register_privilege_overrides
from gateway import catalog, handlers, ledger, server


@pytest.fixture()
def iso_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the ledger at an isolated tmp file and reset env knobs to defaults."""
    path = tmp_path / "gateway_ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    for var in (
        "AILIENANT_GATEWAY_RATE_CAP",
        "AILIENANT_GATEWAY_RATE_REFILL_PER_S",
        "AILIENANT_GATEWAY_BUDGET",
        "AILIENANT_GATEWAY_CALLER_ID",
        "AILIENANT_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture()
def registered() -> Iterator[None]:
    saved = dict(server._HANDLERS)
    for name, handler in handlers.CAPABILITY_HANDLERS.items():
        server.register_handler(name, handler)
    try:
        yield
    finally:
        server._HANDLERS.clear()
        server._HANDLERS.update(saved)


def _dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    result = asyncio.run(server.dispatch_call(name, args))
    return json.loads(result[0].text)  # type: ignore[no-any-return]


_V1_VERBS = {
    "run_task",
    "run_benchmark",
    "check_task_status",
    "get_report",
    "query_memory",
    "get_dependents",
    "get_workspace_graph",
}


# ── 1. List the catalog (the contract is discoverable) ────────────────────────


def test_lists_catalog() -> None:
    tools = catalog.to_mcp_tools()
    assert {t.name for t in tools} == _V1_VERBS
    for tool in tools:
        assert tool.meta is not None
        assert tool.meta["protocol_version"] == catalog.PROTOCOL_VERSION
        assert tool.meta["schema_version"] == catalog.SCHEMA_VERSION
        assert tool.meta["tier"] in {t.value for t in ToolPrivilegeTier}


# ── 2. Run a READ_ONLY verb ───────────────────────────────────────────────────


def test_read_only_verb_responds(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_snippets(
        self: Any, user_input: str, workspace_hash: str = "", k: int = 5
    ) -> Any:
        return [("a.py", "snippet-a")]

    import core.memory.semantic_memory as sm

    monkeypatch.setattr(sm.SemanticMemoryManager, "search_snippets", _fake_snippets)

    payload = _dispatch("query_memory", {"query": "where", "workspace_root": "/ws"})
    assert payload["status"] == "ok"
    assert payload["result"] == [{"file_path": "a.py", "snippet": "snippet-a"}]


# ── 3. A DANGEROUS verb is denied + reported, and never hangs ──────────────────


def test_dangerous_verb_denied_and_reported_without_hanging(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    danger = catalog.Capability(
        name="dod_danger_verb",
        description="hypothetical irreversible verb",
        tier=ToolPrivilegeTier.DANGEROUS,
        input_schema={"type": "object"},
        is_async=False,
    )
    monkeypatch.setattr(catalog, "CATALOG", catalog.CATALOG + (danger,))
    register_privilege_overrides({danger.name: ToolPrivilegeTier.DANGEROUS})

    async def _call() -> Dict[str, Any]:
        # wait_for proves the degrade returns promptly — it never blocks on a human.
        result = await asyncio.wait_for(
            server.dispatch_call("dod_danger_verb", {}), timeout=2.0
        )
        return json.loads(result[0].text)  # type: ignore[no-any-return]

    payload = asyncio.run(_call())
    assert payload["status"] == "denied"
    assert payload["reason"] == "requires_human_approval"
    assert payload["would_have_required"] == "human_approval"
    assert "message" in payload
