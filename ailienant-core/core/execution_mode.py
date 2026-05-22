from typing import Literal

ExecutionModeChoice = Literal["AUTO", "SEQUENTIAL", "MICRO_SWARM", "FULL_SWARM"]

_current: ExecutionModeChoice = "AUTO"


def get_mode() -> ExecutionModeChoice:
    return _current


def set_mode(mode: ExecutionModeChoice) -> None:
    global _current
    _current = mode
