"""The Effort Budget preference — light/balanced/deep verification depth.

Replaces the old SEQUENTIAL/MICRO_SWARM/FULL_SWARM topology selector (plus its
AUTO hardware-suggested variant): that choice never controlled anything the
main graph actually ran (no routing decision in brain/engine.py ever consulted
it), and unlike a topology choice, effort genuinely needs no hardware
capability gate — "how much verification to run" costs local generation time,
not VRAM, so there is nothing here for a host's hardware to make infeasible.
See brain/retry_policy.py for what each level actually controls.
"""
from typing import Literal, cast

from brain.retry_policy import DEFAULT_EFFORT_LEVEL, normalize_effort_level

EffortModeChoice = Literal["light", "balanced", "deep"]

_current: EffortModeChoice = cast(EffortModeChoice, DEFAULT_EFFORT_LEVEL)


def get_effort_level() -> EffortModeChoice:
    return _current


def set_effort_level(level: str) -> None:
    global _current
    # normalize_effort_level's return type is `str` (it is shared with plain
    # dict/state-channel callers that have no Literal to narrow to) but its own
    # contract guarantees a value from the exact same three-member set as
    # EffortModeChoice — the cast documents that guarantee rather than
    # silencing an unrelated type error.
    _current = cast(EffortModeChoice, normalize_effort_level(level))
