"""Ablation arms and their harness-boundary toggles.

Each arm degrades part of the agent pipeline so the benchmark can attribute
value to a specific capability:

* G1 removes all retrieval (zero-shot control),
* G2 removes graph topology but keeps vector retrieval,
* G3 keeps GraphRAG but disables the ReAct self-correction loop,
* G4 is the full pipeline (no toggle).

The toggles are applied with scoped patches entered immediately before a single
task run and exited immediately after. The production code never reads a
benchmark flag. Because the patches are process-global, the runner executes
problems serially so no concurrent coroutine can observe a patched symbol. This
patch-based mechanism is the scaffold's approach; a later phase replaces it with
dependency-injected retrieval strategies.
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
    # Reserved: G4's pipeline with the provider forced to the cloud tier. The
    # provider override is wired by the runner in a later phase, not here.
    G4_FORCE_CLOUD = "G4_FORCE_CLOUD"


# arm -> (mechanism, availability). Inventories each toggle and records whether
# it reuses an existing seam or is net-new harness code.
ARM_TOGGLE_INVENTORY: Dict[AblationArm, Tuple[str, str]] = {
    AblationArm.G1: ("suppress graph and vector retrieval (planner + coder)", "net-new"),
    AblationArm.G2: ("suppress graph topology, keep vector retrieval", "net-new"),
    AblationArm.G3: ("force the one-shot coder (no ReAct iteration)", "existing"),
    AblationArm.G4: ("full pipeline, no toggle", "existing"),
    AblationArm.G4_FORCE_CLOUD: ("force the cloud provider tier", "existing"),
}

# Fully-qualified seams a toggle patches. Exposed as constants so tests can assert
# exactly which symbols each arm touches.
GRAPH_SEAM = "core.memory.graphrag_extractor.GraphRAGDynamicExtractor.deep_parse"
PLANNER_VECTOR_SEAM = "core.memory.semantic_memory.SemanticMemoryManager.search_with_paths"
CODER_VECTOR_SEAM = "core.memory.semantic_memory.SemanticMemoryManager.search_snippets"
CODER_TARGET_SEAM = "brain.engine._coder_target"


async def _empty_deep_parse(self: Any, *args: Any, **kwargs: Any) -> Any:
    """Replacement for deep_parse that yields no graph context."""
    from core.memory.graphrag_extractor import DeepParseResult

    return DeepParseResult(
        target_files=[],
        parsed_files=[],
        context_block="",
        coverage_ratio=0.0,
        token_count=0,
    )


async def _empty_search_with_paths(
    self: Any, *args: Any, **kwargs: Any
) -> Tuple[float, List[str], List[str]]:
    """Replacement for search_with_paths that retrieves nothing."""
    return 0.0, [], []


async def _empty_search_snippets(
    self: Any, *args: Any, **kwargs: Any
) -> List[Tuple[str, str]]:
    """Replacement for search_snippets that retrieves nothing."""
    return []


def _force_one_shot(step: Any) -> str:
    """Replacement for _coder_target that always routes to the one-shot coder."""
    return "coder_agent"


@contextlib.contextmanager
def apply_arm(arm: AblationArm) -> Iterator[None]:
    """Apply an arm's degradation for the duration of a single task run.

    Use strictly as a ``with`` block wrapping exactly one task call. The patches
    restore the original symbols on exit, including on error.
    """
    patches: List[Any] = []
    if arm is AblationArm.G3:
        patches.append(mock.patch(CODER_TARGET_SEAM, _force_one_shot))
    elif arm is AblationArm.G2:
        patches.append(mock.patch(GRAPH_SEAM, _empty_deep_parse))
    elif arm is AblationArm.G1:
        patches.append(mock.patch(GRAPH_SEAM, _empty_deep_parse))
        patches.append(mock.patch(PLANNER_VECTOR_SEAM, _empty_search_with_paths))
        patches.append(mock.patch(CODER_VECTOR_SEAM, _empty_search_snippets))
    # G4 and G4_FORCE_CLOUD apply no retrieval patch here.
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield
