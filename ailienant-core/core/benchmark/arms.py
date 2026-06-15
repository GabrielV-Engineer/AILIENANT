"""Ablation arms and their harness-boundary toggles.

Each arm degrades part of the agent pipeline so the benchmark can attribute
value to a specific capability:

* G1 removes all retrieval (zero-shot control),
* G2 removes graph topology but keeps vector retrieval,
* G3 keeps GraphRAG but disables the ReAct self-correction loop,
* G4 is the full pipeline (no toggle),
* G4_FORCE_CLOUD is G4 with the cloud provider forced via the routing seam.

The toggles are applied with scoped patches entered immediately before a single
task run and exited immediately after. The production code never reads a
benchmark flag. Because the patches are process-global, the runner executes
problems serially so no concurrent coroutine can observe a patched symbol.
"""
from __future__ import annotations

import contextlib
from enum import Enum
from typing import Any, Dict, Iterator, List, Tuple
from unittest import mock


class AblationArm(str, Enum):
    """The experimental arms of the ablation matrix."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"
    G4_FORCE_CLOUD = "G4_FORCE_CLOUD"


# arm -> (mechanism, availability). Inventories each toggle and records whether
# it reuses an existing seam or is net-new harness code.
ARM_TOGGLE_INVENTORY: Dict[AblationArm, Tuple[str, str]] = {
    AblationArm.G1: ("suppress graph and vector retrieval (planner + coder)", "net-new"),
    AblationArm.G2: ("suppress graph topology, keep vector retrieval", "net-new"),
    AblationArm.G3: ("force the one-shot coder (no ReAct iteration)", "existing"),
    AblationArm.G4: ("full pipeline, no toggle", "existing"),
    AblationArm.G4_FORCE_CLOUD: (
        "force cloud tier via derive_routing_decision",
        "existing",
    ),
}

# Fully-qualified seams a toggle patches. Exposed as constants so tests can assert
# exactly which symbols each arm touches.
GRAPH_SEAM = "core.memory.graphrag_extractor.GraphRAGDynamicExtractor.deep_parse"
PLANNER_VECTOR_SEAM = "core.memory.semantic_memory.SemanticMemoryManager.search_with_paths"
CODER_VECTOR_SEAM = "core.memory.semantic_memory.SemanticMemoryManager.search_snippets"
CODER_TARGET_SEAM = "brain.engine._coder_target"
PROVIDER_SEAM = "agents.planner.derive_routing_decision"


def _force_one_shot(step: Any) -> str:
    """Replacement for _coder_target that always routes to the one-shot coder."""
    return "coder_agent"


def _force_cloud_routing(tci: float, css: float) -> str:
    """Replacement for derive_routing_decision that always returns the cloud tier."""
    return "CLOUD"


@contextlib.contextmanager
def apply_arm(arm: AblationArm) -> Iterator[None]:
    """Apply an arm's degradation for the duration of a single task run.

    Use strictly as a ``with`` block wrapping exactly one task call. The patches
    restore the original symbols on exit, including on error.
    """
    from core.benchmark.strategies import (
        VectorOnlyRetrievalStrategy,
        ZeroShotRetrievalStrategy,
    )

    patches: List[Any] = []
    if arm is AblationArm.G3:
        patches.append(mock.patch(CODER_TARGET_SEAM, _force_one_shot))
    elif arm is AblationArm.G2:
        patches = VectorOnlyRetrievalStrategy().patches()
    elif arm is AblationArm.G1:
        patches = ZeroShotRetrievalStrategy().patches()
    elif arm is AblationArm.G4_FORCE_CLOUD:
        patches.append(mock.patch(PROVIDER_SEAM, _force_cloud_routing))
    # G4 applies no patches.
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield
