"""Adaptive tool selection — eager injection, the discovery hatch, and role union.

Three defects this locks shut, each previously invisible to the suite:

  UNION  A tool cross-listed by two families was UPSERTED by name, so whichever
         registrar ran last won and its roles NARROWED instead of merging. The
         same collision existed one layer down in ``resolve_tools``'s
         ``dict.update`` chain, so fixing only the store left the dispatcher
         still denying the other role.

  EAGER  ``brain/agentic_cell.py`` called ``select_tools`` directly, bypassing
         ``DeferredToolLoader``. Its dispatcher map is built from exactly the
         schemas it advertises, so an off-menu name died at lookup with
         "tool not found" and ``tool_search`` — the only way out — could reach
         the cell only by out-ranking real tools on cosine similarity. Combined,
         the cell had no escape hatch at all.

  REPLAY ``tool_search`` tells the model to name a tool next turn so its schema
         gets injected. The cell re-enters as a fresh graph node per iteration
         and used to re-select against the FROZEN ``user_input``, so the next
         iteration's catalog was byte-identical and the instruction could never
         come true. The query is now recorded and replayed.

Also covers the PLAN/PLAN_ONLY normalization gap in the loader's eager baseline
and the "advertised set == dispatchable set" invariant (``filter_resolvable``).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Set

import pytest

import brain.agentic_cell as ac
from brain.agentic_cell import ToolCall, run_agentic_cell_node
from core.deferred_tool_loader import DeferredToolLoader
from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.tool_rag import TOOL_RAG_TOP_K, ToolRAGStore, ToolSchema, populate_tool_catalog
from core.tool_registry import (
    _INTENTIONALLY_UNREGISTERED,
    filter_resolvable,
    resolve_tools,
)

from tests.test_phase7_19_2_agentic_cell import (
    StubAdapter,
    StubSession,
    StubSyncSurface,
    _apply,
    _base_state,
    _config,
    _reasoner_from,
)

CROSS_LISTED = ("architecture_digest", "find_symbol_callers", "trace_cross_boundary")


async def _fake_embed(_text: str) -> List[float]:
    return [0.0] * 1536


def _isolated_store() -> ToolRAGStore:
    return ToolRAGStore(embed_fn=_fake_embed)


def _schema(
    name: str,
    roles: Set[str],
    tier: ToolPrivilegeTier = ToolPrivilegeTier.READ_ONLY,
    *,
    schema_chars: int = 2,
) -> ToolSchema:
    """``schema_chars`` pads json_schema to a realistic size.

    The eager/deferred decision is made on json_schema LENGTH, so a catalog of
    ``"{}"`` stubs fits any window and would silently test the eager branch
    twice. Tests that need the deferred branch must pad to a realistic weight.
    """
    body = "x" * max(0, schema_chars - 2)
    return ToolSchema(
        name=name,
        description=f"Capability provided by {name}.",
        json_schema="{" + body + "}",
        privilege_tier=tier,
        allowed_roles=frozenset(roles),
    )


def setup_function(_func: Any) -> None:
    ac._session_registry.clear()


# =====================================================================
# UNION — cross-listed tools merge roles instead of narrowing them
# =====================================================================


def test_union_disjoint_roles_merge_on_reregistration() -> None:
    """The core defect: two registrars, disjoint roles, one name."""
    store = _isolated_store()
    asyncio.run(store.register_schema(_schema("shared_tool", {"analyst"})))
    asyncio.run(store.register_schema(_schema("shared_tool", {"researcher"})))

    stored = {s.name: s for s in store.all_schemas()}
    assert len(store.all_schemas()) == 1, "name-keyed upsert must not duplicate the row"
    assert stored["shared_tool"].allowed_roles == frozenset({"analyst", "researcher"})

    # Both roles must actually retrieve it — the narrowing was only observable here.
    for role in ("analyst", "researcher"):
        selected = asyncio.run(
            store.select_tools(
                "capability", k=TOOL_RAG_TOP_K, active_role=role,
                session_mode=SessionPermissionMode.DEFAULT,
            )
        )
        assert {s.name for s in selected} == {"shared_tool"}, f"role {role} lost the tool"


def test_union_is_idempotent_with_itself() -> None:
    """Re-registering identical content twice is still a no-op (union with self)."""
    store = _isolated_store()
    for _ in range(3):
        asyncio.run(store.register_schema(_schema("solo", {"core_dev"})))
    rows = store.all_schemas()
    assert len(rows) == 1
    assert rows[0].allowed_roles == frozenset({"core_dev"})


def test_union_tier_conflict_keeps_the_stricter_tier() -> None:
    """A tier disagreement is a definition conflict, not sharing — never downgrade."""
    store = _isolated_store()
    asyncio.run(store.register_schema(_schema("conflicted", {"a"}, ToolPrivilegeTier.EXECUTE)))
    asyncio.run(store.register_schema(_schema("conflicted", {"b"}, ToolPrivilegeTier.READ_ONLY)))

    stored = store.all_schemas()[0]
    assert stored.privilege_tier is ToolPrivilegeTier.EXECUTE, (
        "a later READ_ONLY registration must not downgrade an EXECUTE tool"
    )
    assert stored.allowed_roles == frozenset({"a", "b"})


def test_union_real_catalog_no_registrar_loses_a_role() -> None:
    """Class-level invariant: the combined store's roles equal the union across
    every registrar run in isolation.

    This is the test that would have caught the original bug. It generalises —
    any future cross-listing collision fails here without anyone remembering to
    write a targeted assertion for it.
    """
    from core.tool_rag import populate_tool_catalog as _populate

    combined = _isolated_store()
    reported = asyncio.run(_populate(combined))
    rows = {s.name: s for s in combined.all_schemas()}

    # The registrar-count vs row-count gap IS the collision count — cheapest
    # possible detector, and it needs no per-name knowledge.
    assert reported >= len(rows), "more rows than registrations is impossible"
    collisions = reported - len(rows)
    assert collisions == len(CROSS_LISTED), (
        f"expected exactly {len(CROSS_LISTED)} cross-listed name(s); found {collisions}. "
        "A new collision appeared — confirm it merges roles rather than narrowing them."
    )

    expected: Dict[str, Set[str]] = {}
    for registrar in _registrars():
        solo = _isolated_store()
        asyncio.run(registrar(solo))
        for schema in solo.all_schemas():
            expected.setdefault(schema.name, set()).update(schema.allowed_roles)

    for name, roles in expected.items():
        assert set(rows[name].allowed_roles) == roles, (
            f"{name}: combined store has {sorted(rows[name].allowed_roles)}, "
            f"union across registrars is {sorted(roles)} — a registrar lost a role."
        )


def test_union_cross_listed_trio_reaches_both_roles_at_dispatch() -> None:
    """The SECOND layer: resolve_tools' merge chain must union roles too, or
    classify() denies the analyst even with the store corrected."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    by_name = {s.name: s for s in store.all_schemas()}

    state: Dict[str, Any] = {"workspace_root": "/work", "project_id": "p"}
    resolved = resolve_tools([by_name[n] for n in CROSS_LISTED], state)

    for name in CROSS_LISTED:
        assert {"analyst", "researcher"} <= set(resolved[name].allowed_roles), (
            f"{name} resolved with roles {sorted(resolved[name].allowed_roles)} — "
            "the dict.update chain narrowed it back down."
        )


def _registrars() -> List[Any]:
    from tools.analyst_tools import register_analyst_tools
    from tools.coder_tools import register_coder_tools
    from tools.control_tools import register_control_tools
    from tools.execution_tools import register_execution_tools
    from tools.gateway_tools import register_gateway_tools
    from tools.meta_tools import register_meta_tools
    from tools.mutation_tools import register_mutation_tools
    from tools.orchestrator_tools import register_orchestrator_tools
    from tools.perception_tools import register_perception_tools
    from tools.planner_tools import register_planner_tools
    from tools.researcher_tools import register_researcher_tools
    from tools.universal_tools import register_universal_tools

    return [
        register_analyst_tools, register_coder_tools, register_control_tools,
        register_execution_tools, register_gateway_tools, register_meta_tools,
        register_mutation_tools, register_orchestrator_tools, register_perception_tools,
        register_planner_tools, register_researcher_tools, register_universal_tools,
    ]


# =====================================================================
# PLAN_ONLY — the eager baseline must narrow for the canonical value too
# =====================================================================


@pytest.mark.parametrize("mode", [SessionPermissionMode.PLAN, SessionPermissionMode.PLAN_ONLY])
def test_plan_only_eager_baseline_is_read_only(mode: SessionPermissionMode) -> None:
    """The loader gated on the DEPRECATED ``PLAN`` alias while the frontend sends
    ``PLAN_ONLY``, so in production the narrowing silently never applied. Harmless
    while the eager branch was dead; a WRITE/EXECUTE leak the moment it went live."""
    store = _isolated_store()
    asyncio.run(store.register_schema(_schema("reader", {"core_dev"}, ToolPrivilegeTier.READ_ONLY)))
    asyncio.run(store.register_schema(_schema("writer", {"core_dev"}, ToolPrivilegeTier.WRITE)))
    asyncio.run(store.register_schema(_schema("runner", {"core_dev"}, ToolPrivilegeTier.EXECUTE)))

    decision = asyncio.run(
        DeferredToolLoader(store).resolve(
            "anything", active_role="core_dev", session_mode=mode,
            context_window=10_000_000,  # force eager
        )
    )
    assert decision.mode == "eager"
    assert {s.name for s in decision.schemas} == {"reader"}, (
        f"{mode.value} must narrow the eager baseline to READ_ONLY; "
        f"got {sorted(s.name for s in decision.schemas)}"
    )


# =====================================================================
# RESOLVABLE — advertised set == dispatchable set
# =====================================================================


def test_filter_resolvable_matches_what_resolve_tools_produces() -> None:
    """``resolve_tools`` silently omits _INTENTIONALLY_UNREGISTERED names (they are
    reasoned exclusions, so they must not raise). Any caller that ADVERTISES its
    selection therefore has to filter first, or it promises tools that dispatch
    answers with "not found" — the "tool that lies" defect."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    all_schemas = store.all_schemas()
    state: Dict[str, Any] = {"workspace_root": "/work", "project_id": "p"}

    for role in ("core_dev", "qa_tester", "devops_infra", "doc_manager", "analyst"):
        visible = [s for s in all_schemas if role in s.allowed_roles]
        advertised = filter_resolvable(visible)

        assert not {s.name for s in advertised} & set(_INTENTIONALLY_UNREGISTERED), (
            f"role {role}: advertised an intentionally-unregistered tool"
        )
        assert {s.name for s in advertised} == set(resolve_tools(visible, state)), (
            f"role {role}: advertised set diverges from the dispatchable set"
        )


def test_filter_resolvable_actually_drops_something_for_core_dev() -> None:
    """Guard against the invariant above passing vacuously."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    visible = [s for s in store.all_schemas() if "core_dev" in s.allowed_roles]
    dropped = {s.name for s in visible} - {s.name for s in filter_resolvable(visible)}
    assert "file_write" in dropped, (
        "core_dev's slice is expected to contain at least one phantom name; "
        f"dropped={sorted(dropped)}"
    )


# =====================================================================
# EAGER — the cell's behavioral change
# =====================================================================


def _capturing_reasoner(scripts: List[List[ToolCall]], sink: List[Any]) -> ac.CellReasoner:
    """Reasoner that records the messages it was handed, so the advertised tool
    hint can be asserted directly rather than inferred from dispatch outcomes."""
    calls = {"i": 0}

    async def _reason(messages: Any) -> List[ToolCall]:
        sink.append(messages)
        idx = min(calls["i"], len(scripts) - 1)
        calls["i"] += 1
        return scripts[idx]

    return _reason


def _hint_of(messages: List[Dict[str, str]]) -> str:
    for msg in messages:
        if msg.get("role") == "system" and "Additional tools are available" in msg.get("content", ""):
            return msg["content"]
    return ""


_CELL_SCHEMA_CHARS: int = 400
"""Roughly the real catalog's mean json_schema size (19,791 chars / 53 rows)."""


def _seed_cell_catalog(store: ToolRAGStore) -> List[str]:
    """Eight resolvable core_dev tools — more than TOOL_RAG_TOP_K, so eager and
    deferred are distinguishable by advertised count alone. Realistically sized
    so a small context window genuinely forces the deferred branch."""
    names = [
        "todo_write", "tool_search", "validate_ast", "security_audit",
        "sandbox_bash", "check_type_integrity", "task_get", "task_create",
    ]
    for name in names:
        asyncio.run(
            store.register_schema(
                _schema(name, {"core_dev"}, schema_chars=_CELL_SCHEMA_CHARS)
            )
        )
    return names


def _profile_state(context_window: int, **overrides: Any) -> Dict[str, Any]:
    return _base_state(
        active_llm_profile=SimpleNamespace(context_window=context_window),
        **overrides,
    )


def test_eager_advertises_the_whole_role_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large context window injects every visible tool — no embedding round-trip,
    no cap. This is the behavior the cell never had."""
    store = _isolated_store()
    names = _seed_cell_catalog(store)
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    seen: List[Any] = []
    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _capturing_reasoner([[ToolCall("todo_write", {"todos": []})]], seen)

    asyncio.run(run_agentic_cell_node(_profile_state(200_000), _config(adapter, reasoner)))

    hint = _hint_of(seen[0])
    advertised = {n for n in names if f"- {n}:" in hint}
    assert advertised == set(names), f"eager must advertise all {len(names)}; got {sorted(advertised)}"
    assert len(advertised) > TOOL_RAG_TOP_K
    assert "COMPLETE tool set" in hint, "eager must not invite further tool_search discovery"


def test_deferred_keeps_the_cap_and_the_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small window retrieves instead, and tool_search always survives — the
    escape hatch that made the top-k a hard wall by its absence."""
    store = _isolated_store()
    names = _seed_cell_catalog(store)
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    seen: List[Any] = []
    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _capturing_reasoner([[ToolCall("todo_write", {"todos": []})]], seen)

    asyncio.run(run_agentic_cell_node(_profile_state(2048), _config(adapter, reasoner)))

    hint = _hint_of(seen[0])
    advertised = {n for n in names if f"- {n}:" in hint}
    assert 0 < len(advertised) <= TOOL_RAG_TOP_K + 1
    assert len(advertised) < len(names), "small window must NOT inject the whole slice"
    assert "tool_search" in advertised, "the discovery hatch must survive the deferred cut"
    # The cell asks for k+1 so the reserved hatch slot does not cost a usable tool.
    assert len(advertised - {"tool_search"}) == TOOL_RAG_TOP_K


def test_cell_never_advertises_a_phantom_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under eager the whole slice is injected, so an unfiltered hint would offer
    file_write / atomic_code_patch and then answer "tool not found"."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    seen: List[Any] = []
    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _capturing_reasoner([[ToolCall("file_write", {"path": "x", "content": "y"})]], seen)

    delta = asyncio.run(run_agentic_cell_node(_profile_state(200_000), _config(adapter, reasoner)))

    hint = _hint_of(seen[0])
    for phantom in ("file_write", "atomic_code_patch"):
        assert f"- {phantom}:" not in hint, f"{phantom} advertised but never dispatchable"
    # And naming it anyway still degrades safely rather than crashing the node.
    assert delta["agentic_trajectory"]


def test_cell_never_offers_a_loop_unsafe_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """toggle_plan_mode rewrites session_permission_mode, but ToolDispatcher pins its
    mode at construction and nothing promotes that channel — so from a loop the call
    reports success and changes nothing. Retrieval kept it out of the cell only by
    accident; whole-catalog injection would make it always visible."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    seen: List[Any] = []
    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _capturing_reasoner([[ToolCall("toggle_plan_mode", {"mode": "AUTO"})]], seen)
    # Seeded deliberately NOT equal to the mode the scripted call requests: with both
    # set to AUTO the final assertion would hold whether the filter works or not.
    state = _profile_state(200_000, session_permission_mode="CAUTIOUS")

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert "- toggle_plan_mode:" not in _hint_of(seen[0])
    observations = [
        m.get("content", "") for m in delta["agentic_trajectory"]
        if isinstance(m, dict) and "[toggle_plan_mode]" in m.get("content", "")
    ]
    assert observations and "not found" in observations[0], (
        "the filter must remove it from the dispatcher map, not merely unadvertise it"
    )
    assert state["session_permission_mode"] == "CAUTIOUS", (
        "the refused call must not have rewritten the permission channel"
    )


# =====================================================================
# LOOP-SAFE — one predicate, every dispatch-loop consumer
# =====================================================================


def test_filter_loop_safe_drops_only_the_loop_unsafe_names() -> None:
    """Guard against a predicate that is too broad — it must remove the named
    tools and nothing else, preserving order."""
    from core.tool_registry import _NO_AUTONOMOUS_LOOP, filter_loop_safe

    schemas = [
        _schema("read_file", {"core_dev"}),
        _schema("toggle_plan_mode", {"core_dev"}),
        _schema("validate_ast", {"core_dev"}),
    ]
    kept = filter_loop_safe(schemas)

    assert [s.name for s in kept] == ["read_file", "validate_ast"]
    assert "toggle_plan_mode" in _NO_AUTONOMOUS_LOOP
    assert filter_loop_safe([]) == []


def test_resolve_tools_still_builds_a_loop_unsafe_tool_when_asked_directly() -> None:
    """The exclusion is consumer-side, applied BEFORE resolve_tools. resolve_tools
    itself must keep serving its direct, non-loop callers unchanged — pinned by
    tests/test_tool_registry.py's dispatchability contract for the same name."""
    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    schema = next(s for s in store.all_schemas() if s.name == "toggle_plan_mode")

    resolved = resolve_tools([schema], {"workspace_root": "/work", "project_id": "p"})

    assert "toggle_plan_mode" in resolved


def test_subagent_worker_never_resolves_a_loop_unsafe_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE REGRESSION ROW. Routing the subagent through DeferredToolLoader made
    eager injection hand it the whole role slice, so a loop-unsafe tool went from
    'surfaced only if it won a top-k ranking' to 'present deterministically'."""
    from brain.nodes.subagent_worker_node import _resolve_tools

    store = _isolated_store()
    asyncio.run(populate_tool_catalog(store))
    # _resolve_tools imports the module-level tool_rag_store singleton, exactly
    # like the agentic cell — same monkeypatch seam.
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    state: Dict[str, Any] = {
        "workspace_root": "/work",
        "project_id": "p",
        "active_llm_profile": SimpleNamespace(context_window=200_000),  # force eager
    }
    tools = asyncio.run(
        _resolve_tools(
            "core_dev", state,
            intent="add a unit test for the parser",
            session_mode=SessionPermissionMode.DEFAULT,
        )
    )

    assert tools, "fixture must actually resolve a non-empty arsenal"
    assert "toggle_plan_mode" not in tools, (
        "eager injection put a loop-unsafe tool in the subagent's dispatcher map"
    )
    # Non-vacuous: the same eager slice still carries ordinary tools.
    assert len(tools) > TOOL_RAG_TOP_K


def test_coder_grounding_pre_pass_never_offers_a_loop_unsafe_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing exposure, independent of the eager wiring: the pre-pass filters
    to READ_ONLY, which a loop-unsafe tool passes. `select_tools`'s READ_ONLY-survivor
    guarantee actively promotes one into the selection when the ranking has no other
    READ_ONLY candidate, so this loop is more exposed than the tier filter suggests."""
    from tests.test_coder_tool_grounding import (
        _fake_llm_response,
        _make_state,
        _make_step,
        _run_with_mocks,
    )

    store = _isolated_store()
    for name in ("toggle_plan_mode", "validate_ast"):
        asyncio.run(store.register_schema(_schema(name, {"core_dev"})))
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    step = _make_step(action="write_file", target_file="brand_new_module.py")
    state = _make_state(step)

    _result, calls = asyncio.run(
        _run_with_mocks(
            state,
            file_content=None,      # new file + empty RAG => grounding fires
            rag_snippets=[],
            ainvoke_responses=[_fake_llm_response("{}"), _fake_llm_response("")],
        )
    )

    assert len(calls) >= 2, "the grounding pre-pass must have fired"
    advertised = "".join(
        m.get("content", "") for m in calls[0]["messages"] if isinstance(m, dict)
    )
    assert "toggle_plan_mode" not in advertised
    assert "validate_ast" in advertised, (
        "non-vacuous: the pre-pass must still offer ordinary READ_ONLY tools"
    )


# =====================================================================
# REPLAY — tool_search's "name it next turn" becomes true by construction
# =====================================================================


def test_tool_search_query_is_recorded_for_the_next_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _isolated_store()
    _seed_cell_catalog(store)
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("tool_search", {"query": "audit dependencies"})]])

    delta = asyncio.run(run_agentic_cell_node(_profile_state(2048), _config(adapter, reasoner)))

    assert delta["cell_tool_query"] == "audit dependencies"


def test_recorded_query_is_cleared_once_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale query must not keep re-selecting forever — the channel is a
    consumed-and-cleared scalar, not an accumulator."""
    store = _isolated_store()
    _seed_cell_catalog(store)
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    adapter = StubAdapter(StubSession(exit_codes=[0], outputs=[b"ok\n"]), StubSyncSurface())
    reasoner = _reasoner_from([[ToolCall("todo_write", {"todos": []})]])
    state = _profile_state(2048, cell_tool_query="audit dependencies")

    delta = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    assert delta["cell_tool_query"] is None


def test_replayed_query_injects_a_tool_the_frozen_intent_would_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end shift-left contract across two iterations: search, then name
    the tool, and it dispatches instead of reporting "not found"."""
    store = _isolated_store()
    # A deliberately large deferred catalog so the cap genuinely bites.
    wanted = "security_audit"
    for name in (
        "todo_write", "tool_search", "validate_ast", "sandbox_bash",
        "check_type_integrity", "task_get", "task_create", wanted,
    ):
        asyncio.run(
            store.register_schema(
                _schema(name, {"core_dev"}, schema_chars=_CELL_SCHEMA_CHARS)
            )
        )
    monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

    adapter = StubAdapter(StubSession(exit_codes=[0, 0], outputs=[b"ok\n", b"ok\n"]), StubSyncSurface())
    reasoner = _reasoner_from([
        [ToolCall("tool_search", {"query": f"Capability provided by {wanted}."})],
        [ToolCall(wanted, {})],
    ])
    state = _profile_state(2048)

    first = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))
    assert first["cell_tool_query"], "iteration 1 must record the query"
    _apply(state, first)

    second = asyncio.run(run_agentic_cell_node(state, _config(adapter, reasoner)))

    observations = [
        m.get("content", "") for m in second["agentic_trajectory"]
        if isinstance(m, dict) and f"[{wanted}]" in m.get("content", "")
    ]
    assert observations, f"iteration 2 never dispatched {wanted}"
    assert "not found" not in observations[0], (
        f"{wanted} was listed by tool_search but the replay failed to inject it — "
        "the shift-left instruction is still a promise the cell cannot keep."
    )


# =====================================================================
# INTENT — composed per iteration, and pure
# =====================================================================


def test_compose_cell_intent_is_pure() -> None:
    """LangGraph replays this node on resume/Rewind; a non-pure intent would make
    a resumed run select different tools than the original."""
    state = _base_state(
        agentic_trajectory=[
            {"iteration": 0, "edits": ["a.py", "b.py"], "diagnostics": "2 failed"},
            {"iteration": 1, "edits": ["c.py"], "diagnostics": "1 failed"},
        ],
    )
    assert ac._compose_cell_intent(state) == ac._compose_cell_intent(state)


def test_compose_cell_intent_tracks_progress() -> None:
    """The whole point: iteration 15 must not retrieve against iteration 1's query."""
    fresh = _base_state()
    advanced = _base_state(
        agentic_trajectory=[
            {"iteration": 0, "edits": ["auth.py"], "diagnostics": "ImportError: no module named jwt"},
        ],
    )
    assert ac._compose_cell_intent(fresh) != ac._compose_cell_intent(advanced)
    composed = ac._compose_cell_intent(advanced)
    assert "auth.py" in composed
    assert "ImportError" in composed


def test_compose_cell_intent_is_bounded() -> None:
    """Embedding input is still I/O — an unbounded diagnostics splice is capped."""
    state = _base_state(
        agentic_trajectory=[
            {"iteration": i, "edits": [f"f{i}.py"], "diagnostics": "x" * 5_000}
            for i in range(20)
        ],
    )
    assert len(ac._compose_cell_intent(state)) <= ac._CELL_INTENT_MAX_CHARS
