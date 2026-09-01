# ailienant-core/tests/test_graph_path_map_integrity.py
#
# Composition guard for conditional edges: a router may only return values its
# own path-map declares.
#
# LangGraph resolves a router's verdict against the path-map dict at RUNTIME, and
# a missing key raises KeyError only once that branch is actually taken. Nothing
# in ruff/mypy/pyright models the relationship, and a unit test that asserts a
# router's return value in isolation passes while the graph it feeds cannot route
# it -- which is exactly how `route_after_summarize`'s accepted-plan verdict
# ("step_dispatch") shipped against a two-key path-map: approving a plan raised
# KeyError instead of executing it, surfaced to the user as an unrelated
# "make sure a BYOM preset is active" message.
#
# The returnable set is read from the source with `ast` rather than by calling
# each router: a router's branches depend on session mode, state channels and
# permission normalization, so exercising every path would mean rebuilding each
# one's preconditions here -- the enumeration would then drift from the code it
# guards. Every string literal a `return` can yield is a returnable value,
# regardless of the condition guarding it, so the static read is both simpler
# and stricter.

import ast
import inspect
import textwrap
from typing import Any, Dict, Set

import pytest
from langgraph.graph import END

from brain.engine import workflow


def _unwrap(path: Any) -> Any:
    """The plain router function behind LangGraph's RunnableCallable wrapper."""
    return getattr(path, "func", None) or path


def _returned_literals(func: Any) -> Set[str]:
    """Every string a `return` in ``func`` can yield, including via a variable.

    Covers the two shapes the routers use: `return "node"` directly, and the
    `target = "node"` / ... / `return target` shape the telemetry-logging routers
    need. Values that cannot be resolved to a literal are ignored -- this gate
    proves declared keys cover the literals, and is not a general dataflow
    analysis.
    """
    # dedent, not cleandoc: cleandoc re-indents relative to the docstring and
    # corrupts the body. A nested router is indented in its own source slice.
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    assignments: Dict[str, Set[str]] = {}
    returned: Set[str] = set()

    for node in ast.walk(tree):
        # `target = "step_dispatch"` — remember what each name can hold.
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        assignments.setdefault(tgt.id, set()).add(node.value.value)
        # END is imported as a module-level name; resolve it to its own value.
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if node.value.id == "END":
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        assignments.setdefault(tgt.id, set()).add(END)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            returned.add(value.value)
        elif isinstance(value, ast.Name):
            if value.id == "END":
                returned.add(END)
            else:
                returned |= assignments.get(value.id, set())
        elif isinstance(value, ast.IfExp):
            # `return "a" if cond else "b"` — both arms are returnable.
            for arm in (value.body, value.orelse):
                if isinstance(arm, ast.Constant) and isinstance(arm.value, str):
                    returned.add(arm.value)
                elif isinstance(arm, ast.Name):
                    returned.add(END) if arm.id == "END" else returned.update(
                        assignments.get(arm.id, set())
                    )
    return returned


def _conditional_branches() -> list[tuple[str, str, Any, Set[str]]]:
    """(source node, router name, router callable, declared path-map keys)."""
    found: list[tuple[str, str, Any, Set[str]]] = []
    for source, branches in workflow.branches.items():
        for name, branch in branches.items():
            ends = branch.ends
            if ends:
                found.append((str(source), name, _unwrap(branch.path), {str(k) for k in ends}))
    return found


def test_the_graph_actually_has_conditional_edges_to_check() -> None:
    """Fail loudly if introspection stops finding branches, rather than passing
    vacuously — a green run must mean the edges were checked, not skipped."""
    assert len(_conditional_branches()) >= 8


@pytest.mark.parametrize(
    "source,router_name,router,ends",
    _conditional_branches(),
    ids=[f"{src}->{name}" for src, name, _, _ in _conditional_branches()],
)
def test_every_returnable_value_is_declared_in_the_path_map(
    source: str, router_name: str, router: Any, ends: Set[str]
) -> None:
    """A router verdict its own edge cannot route is a runtime KeyError."""
    returnable = _returned_literals(router)
    undeclared = returnable - ends
    assert not undeclared, (
        f"{source} -> {router_name} can return {sorted(undeclared)}, which "
        f"its path-map does not declare (declared: {sorted(ends)}). "
        "Taking that branch raises KeyError at runtime."
    )


def test_route_after_summarize_accepted_plan_verdict_is_routable() -> None:
    """The specific regression: an approved plan routes to execution.

    Kept explicit alongside the parametrized sweep because this is the verdict
    that shipped broken, and it is the one a reader will look for by name.
    """
    from brain.engine import route_after_summarize

    carried = {"task_id": "t1", "project_id": "p1", "mission_spec": object()}
    verdict = route_after_summarize(carried)
    ends = workflow.branches["session_delta_aggregator"]["route_after_summarize"].ends or {}
    assert verdict == "step_dispatch"
    assert verdict in {str(k) for k in ends}
