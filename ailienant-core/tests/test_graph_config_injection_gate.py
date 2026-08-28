"""Graph Config Injection Gate.

Every runtime dependency the cognitive engine hands its nodes — the narration
emitter, the reasoning sink, the activity channel, the cell dispatcher, the
compaction callback — rides on ``config["configurable"]``. LangGraph delivers
that config only to a node whose OUTERMOST registered callable advertises a
parameter it recognises, so a node wrapper is the single point where the whole
seam can be lost at once, silently, with every unit test still green.

That is exactly what happened: both wrappers were variadic
(``async def _wrapped(state, *args, **kwargs)``), which advertises no injectable
parameter, so nothing was ever passed. Every narration/reasoning test kept
passing because each one calls a node function DIRECTLY with a hand-built config
dict, and the timeline gate replaced the compiled graph with a fake that pulled
``narrate`` out of the config it was itself handed. Nothing exercised the path
that was broken.

Rows certified here:

  INJECT1  through a REAL compiled graph, a node wrapped by ``_instrument_node``
           or ``_guarded`` receives the caller's config. The row that did not
           exist.
  INJECT2  negative control — the variadic wrapper shape this gate exists to
           reject genuinely fails INJECT1's assertion, so a regression cannot
           pass by accident.
  INJECT3  derived over the SHIPPED graphs: every node whose implementation
           declares ``config`` is one LangGraph will actually inject into. No
           node list is restated here — the graphs are walked, so a node added
           later is covered without touching this file.
  INJECT4  every such implementation annotates ``config`` with a form LangGraph
           accepts, read from the runtime's own table rather than restated.
  INJECT5  building the graphs emits no config-typing warning.

All async cases run under anyio (asyncio backend).
"""
from __future__ import annotations

import inspect
import warnings
from typing import Any, Callable, Dict, Optional, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from brain.engine import _instrument_node, alienant_app
from brain.ideation import _guarded, ideation_graph
from brain.state import AIlienantGraphState, accepts_config

pytestmark = pytest.mark.anyio

SENTINEL = "injected-by-the-runtime"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _run_wrapped_through_graph(wrapper: Callable[..., Any]) -> Optional[RunnableConfig]:
    """Compile a one-node graph around ``wrapper`` and return the config the node
    actually saw — measured through LangGraph, never handed to it directly."""
    seen: Dict[str, Optional[RunnableConfig]] = {"config": None}

    async def probe(
        state: Dict[str, Any], config: Optional[RunnableConfig] = None
    ) -> Dict[str, Any]:
        seen["config"] = config
        return {}

    workflow: StateGraph[AIlienantGraphState] = StateGraph(AIlienantGraphState)
    workflow.add_node("probe", wrapper("probe", probe))  # type: ignore[type-var]
    workflow.add_edge(START, "probe")
    workflow.add_edge("probe", END)
    app = workflow.compile()

    await app.ainvoke(
        cast(AIlienantGraphState, {"task_id": "gate-inject"}),
        config={"configurable": {"thread_id": "gate-inject", "narrate": SENTINEL}},
    )
    return seen["config"]


def _node_implementation(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Peel this project's own node wrappers down to the node function itself.

    ``_instrument_node``/``_guarded`` hold the wrapped callable in a closure cell
    named ``fn``; the DLQ and reflexion guards inside use ``functools.wraps``, which
    ``inspect.signature`` already sees through. Bounded by an identity set so a
    pathological self-reference cannot spin.
    """
    seen: set[int] = set()
    while getattr(fn, "__name__", "") == "_wrapped" and id(fn) not in seen:
        seen.add(id(fn))
        inner = inspect.getclosurevars(fn).nonlocals.get("fn")
        if not callable(inner):
            break
        fn = cast(Callable[..., Any], inner)
    return fn


def _registered_nodes() -> list[tuple[str, Any, Callable[..., Any]]]:
    """(node name, LangGraph's func_accepts, node implementation) for every node
    in the shipped graphs that LangGraph invokes as a plain callable.

    A ``__start__`` sentinel and a compiled subgraph carry no ``func_accepts`` and
    are skipped — a subgraph receives config through its own nodes, each of which
    this same walk covers via ``ideation_graph``.
    """
    rows: list[tuple[str, Any, Callable[..., Any]]] = []
    for graph in (alienant_app, ideation_graph):
        for name, node in graph.nodes.items():
            if name.startswith("__"):
                continue
            bound = getattr(node, "bound", None)
            func_accepts = getattr(bound, "func_accepts", None)
            if func_accepts is None:
                continue
            impl = getattr(bound, "afunc", None) or getattr(bound, "func", None)
            if impl is None:
                continue
            rows.append((name, func_accepts, _node_implementation(impl)))
    return rows


# --------------------------------------------------------------------------- #
# INJECT1 — the behavioural row that did not exist
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize("wrapper", [_instrument_node, _guarded])
async def test_INJECT1_wrapped_node_receives_config_through_the_compiled_graph(
    wrapper: Callable[..., Any],
) -> None:
    config = await _run_wrapped_through_graph(wrapper)
    assert config is not None, "the node was invoked with no config at all"
    assert config.get("configurable", {}).get("narrate") == SENTINEL, (
        "config arrived but without the caller's configurable payload — the DI "
        "seam every narration/reasoning/activity feature rides on"
    )


# --------------------------------------------------------------------------- #
# INJECT2 — negative control
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_INJECT2_a_variadic_wrapper_fails_the_same_assertion() -> None:
    """The shape this gate exists to reject must actually fail it.

    A gate that passes against the broken code is worthless, and this specific
    wrapper shape shipped for the lifetime of the feature while every other test
    stayed green.
    """

    def variadic_wrapper(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        async def _wrapped(state: Any, *args: Any, **kwargs: Any) -> Any:
            return await fn(state, *args, **kwargs)

        return _wrapped

    config = await _run_wrapped_through_graph(variadic_wrapper)
    assert config is None, (
        "a variadic wrapper unexpectedly received config — LangGraph's injection "
        "rule has changed, and this gate's premise needs rechecking"
    )


# --------------------------------------------------------------------------- #
# INJECT3 / INJECT4 — derived over the shipped graphs
# --------------------------------------------------------------------------- #


def test_INJECT3_every_config_declaring_node_is_one_langgraph_injects_into() -> None:
    offenders = [
        name
        for name, func_accepts, impl in _registered_nodes()
        if accepts_config(impl) and "config" not in func_accepts
    ]
    assert not offenders, (
        "these nodes declare a `config` parameter that LangGraph will never fill — "
        f"every configurable seam is None inside them: {offenders}"
    )


def test_INJECT4_config_annotations_match_the_runtime_accepted_forms() -> None:
    # Read the accepted annotations from the runtime's own table instead of
    # restating them: they are the authority, and a copy here would drift.
    from langgraph._internal._runnable import KWARGS_CONFIG_KEYS

    accepted = next(typ for kw, typ, _, _ in KWARGS_CONFIG_KEYS if kw == "config")

    offenders: list[str] = []
    for name, _func_accepts, impl in _registered_nodes():
        param = inspect.signature(impl).parameters.get("config")
        if param is not None and param.annotation not in accepted:
            offenders.append(f"{name}: {param.annotation!r}")
    assert not offenders, (
        "these nodes annotate `config` with a form LangGraph does not recognise, "
        f"so it would skip them if they were ever registered raw: {offenders}"
    )


# --------------------------------------------------------------------------- #
# INJECT5 — no config-typing warning at build time
# --------------------------------------------------------------------------- #


def test_INJECT5_building_a_graph_emits_no_config_typing_warning() -> None:
    async def wrongly_typed(
        state: Dict[str, Any], config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return {}

    # First prove the warning this row watches for is real and reachable, so a
    # future LangGraph that stopped emitting it cannot make this row vacuous.
    with pytest.warns(UserWarning, match="config"):
        raw: StateGraph[AIlienantGraphState] = StateGraph(AIlienantGraphState)
        raw.add_node("wrongly_typed", wrongly_typed)  # type: ignore[type-var]
        raw.add_edge(START, "wrongly_typed")
        raw.compile()

    # The shipped nodes must not trip it.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        clean: StateGraph[AIlienantGraphState] = StateGraph(AIlienantGraphState)
        for name, _func_accepts, impl in _registered_nodes():
            clean.add_node(name, impl)  # type: ignore[type-var]
        clean.add_edge(START, _registered_nodes()[0][0])
        clean.compile()
