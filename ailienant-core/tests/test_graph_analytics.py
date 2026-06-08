"""GraphRAG enrichment: Louvain communities, edge-confidence derivation, God Nodes.

Exercises the pure analytics units directly (no live WS / process pool / DB):
- the worker ``calculate_graph_analytics_sync`` (PageRank + Louvain + confidence),
- the confidence resolver ``_resolve_edge_confidence``,
- the dashboard's degree-centrality God-node ranking,
- and the backward-compatible defaults on the extended PPRResult contract.
"""
from typing import Dict

from shared.contracts import PPRRequest, PPRResult
from brain.memory import (
    MAX_GRAPH_EDGES,
    calculate_graph_analytics_sync,
    calculate_ppr_sync,
    _resolve_edge_confidence,
)
from api.memory_dashboard import _rank_god_nodes


def _k_clique(prefix: str, n: int) -> list[tuple[str, str]]:
    """All directed edges of an n-node clique (deterministic order)."""
    nodes = [f"{prefix}{i}" for i in range(n)]
    return [(a, b) for a in nodes for b in nodes if a != b]


def test_louvain_separates_two_cliques() -> None:
    """Two 4-cliques joined by a single bridge land in distinct communities."""
    edges = tuple(_k_clique("a", 4) + _k_clique("b", 4) + [("a0", "b0")])
    req = PPRRequest(edges=edges)
    result = calculate_graph_analytics_sync(req)

    assert result.success
    # Every node received a community id.
    assert set(result.communities) == {f"a{i}" for i in range(4)} | {f"b{i}" for i in range(4)}
    # Each clique is internally consistent and the two cliques differ.
    a_comms = {result.communities[f"a{i}"] for i in range(4)}
    b_comms = {result.communities[f"b{i}"] for i in range(4)}
    assert len(a_comms) == 1 and len(b_comms) == 1
    assert a_comms != b_comms


def test_louvain_is_deterministic() -> None:
    """Fixed seed → identical partition across runs (stable colors)."""
    edges = tuple(_k_clique("a", 4) + _k_clique("b", 4) + [("a0", "b0")])
    r1 = calculate_graph_analytics_sync(PPRRequest(edges=edges))
    r2 = calculate_graph_analytics_sync(PPRRequest(edges=edges))
    assert r1.communities == r2.communities


def test_confidence_derivation_three_classes() -> None:
    indexed = ("/p/a.py", "/p/b.py", "/p/utils.py", "/p/sub/utils.py")
    edges = (
        ("/p/a.py", "/p/b.py"),   # target is an indexed source file
        ("/p/a.py", "os"),         # external/unindexed module
        ("/p/a.py", "utils"),      # stem collides with two indexed files
    )
    conf = dict(((s, t), (label, score)) for s, t, label, score in _resolve_edge_confidence(edges, indexed))
    assert conf[("/p/a.py", "/p/b.py")] == ("EXTRACTED", 1.0)
    assert conf[("/p/a.py", "os")] == ("INFERRED", 0.5)
    assert conf[("/p/a.py", "utils")] == ("AMBIGUOUS", 0.25)


def test_confidence_empty_indexed_is_all_inferred() -> None:
    """With no indexed universe, no edge can resolve to a file → all INFERRED."""
    edges = (("a", "b"), ("a", "c"))
    out = _resolve_edge_confidence(edges, ())
    assert all(label == "INFERRED" and score == 0.5 for _, _, label, score in out)


def test_god_nodes_top3_by_degree() -> None:
    candidates = ["hub", "mid1", "mid2", "leaf"]
    in_deg = {"hub": 5, "mid1": 2, "mid2": 1, "leaf": 0}
    out_deg = {"hub": 4, "mid1": 1, "mid2": 2, "leaf": 1}
    ppr: Dict[str, float] = {"hub": 0.4, "mid1": 0.2, "mid2": 0.2, "leaf": 0.1}
    god = _rank_god_nodes(candidates, in_deg, out_deg, ppr)
    # hub (9), mid1 (3), mid2 (3) beat leaf (1).
    assert god == {"hub", "mid1", "mid2"}
    assert "leaf" not in god


def test_god_nodes_degree_tiebreak_is_deterministic() -> None:
    """Equal degree → PPR then id break the tie deterministically."""
    candidates = ["x", "y", "z"]
    in_deg = {"x": 1, "y": 1, "z": 1}
    out_deg = {"x": 1, "y": 1, "z": 1}
    ppr = {"x": 0.3, "y": 0.2, "z": 0.1}
    assert _rank_god_nodes(candidates, in_deg, out_deg, ppr, top_n=2) == {"x", "y"}


def test_pprresult_backward_compat_defaults() -> None:
    """The extended contract defaults the new fields, so old call sites are unaffected.

    Asserted independently of PageRank/scipy availability: the legacy scores-only
    path never populates communities/edge_confidence regardless of success.
    """
    r = calculate_ppr_sync(PPRRequest(edges=(("a", "b"), ("b", "c"))))
    assert r.communities == {}
    assert r.edge_confidence == ()
    # Direct construction also defaults cleanly (old call sites unaffected).
    bare = PPRResult(scores={}, success=True)
    assert bare.communities == {} and bare.edge_confidence == ()


def test_empty_graph_is_safe() -> None:
    r = calculate_graph_analytics_sync(PPRRequest(edges=()))
    assert r.success and r.scores == {} and r.communities == {}


def test_oversized_graph_is_skipped_gracefully() -> None:
    """An edge list past the cap is refused before any graph is built.

    Both builders degrade to an empty, successful result (cap-and-skip) so the
    caller sees no centrality/community data, not an error.
    """
    edges = tuple((str(i), str(i + 1)) for i in range(MAX_GRAPH_EDGES + 1))
    req = PPRRequest(edges=edges)

    ppr = calculate_ppr_sync(req)
    assert ppr.success and ppr.scores == {}

    analytics = calculate_graph_analytics_sync(req)
    assert analytics.success and analytics.scores == {} and analytics.communities == {}


def test_at_cap_boundary_still_computes() -> None:
    """A graph exactly at the cap is built normally — the guard is off-by-one safe."""
    edges = tuple((str(i), str(i + 1)) for i in range(MAX_GRAPH_EDGES))
    assert len(edges) == MAX_GRAPH_EDGES

    ppr = calculate_ppr_sync(PPRRequest(edges=edges))
    assert ppr.success and ppr.scores  # populated, not skipped
