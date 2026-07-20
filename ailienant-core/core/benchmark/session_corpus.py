"""Synthetic long-session corpus generator.

Produces deterministic, seeded synthetic message histories that exercise
run_summarize_node / ContextPipeline.assemble() across a range of session
lengths and message sizes. A pure function of its inputs — no wall-clock or
network dependency — so a fixed seed list reproduces an identical corpus,
mirroring this package's existing reproducibility discipline (fixed-seed
ablation runs in core/benchmark/runner.py).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

_ROLES: Tuple[str, str] = ("user", "assistant")

_WORD_BANK: Tuple[str, ...] = (
    "the", "function", "returns", "a", "value", "when", "state", "is",
    "updated", "class", "method", "raises", "exception", "if", "input",
    "invalid", "token", "budget", "context", "window", "session", "turn",
    "conversation", "history", "compress", "truncate", "layer", "assemble",
    "checkpoint", "graph", "node", "config", "error", "handler", "async",
    "await", "response", "request", "path", "file", "module", "import",
)


@dataclass(frozen=True)
class SyntheticSession:
    """One deterministic synthetic session for a given seed/turn-count/window."""

    session_id: str
    messages: List[Dict[str, str]]
    context_window: int


def generate_session(
    seed: int,
    n_turns: int,
    context_window: int,
    *,
    msg_len_range: Tuple[int, int] = (5, 80),
) -> SyntheticSession:
    """Build one synthetic session with n_turns alternating user/assistant messages."""
    rng = random.Random(seed)
    messages = [
        {
            "role": _ROLES[i % 2],
            "content": " ".join(
                rng.choices(_WORD_BANK, k=rng.randint(*msg_len_range))
            ),
        }
        for i in range(n_turns)
    ]
    return SyntheticSession(
        session_id=f"synth-{seed}-{n_turns}-{context_window}",
        messages=messages,
        context_window=context_window,
    )


def generate_corpus(
    seeds: List[int],
    turn_counts: List[int],
    context_windows: List[int],
) -> List[SyntheticSession]:
    """Full cross-product corpus — deterministic given the same input lists."""
    return [
        generate_session(seed, turns, window)
        for seed in seeds
        for turns in turn_counts
        for window in context_windows
    ]
