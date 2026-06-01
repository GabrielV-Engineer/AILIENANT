"""Cross-turn failure-signature circuit breaker for the self-healing loop.

A single logical defect — a malformed import, an un-stubbable API mismatch — that
recurs every turn must not turn each invocation into another doomed reflexion cycle
burning LLM calls. This breaker keys on a *normalized* failure signature so the same
error collapses to one key regardless of volatile detail (line numbers, hex
addresses, absolute paths). After ``FAILURE_SIGNATURE_THRESHOLD`` repeats the breaker
OPENs and the reflexion node concedes straight to the DLQ.

The map only ever holds currently-failing signatures (a success or an explicit clear
pops the key), so memory is O(distinct failing signatures), never O(history). The
instance is a module-level singleton: unlike the per-(project,file) reactive breaker,
self-healing state must persist *across* graph invocations within the process so a
defect seen last turn is still remembered this turn.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, Tuple

from brain.retry_policy import FAILURE_SIGNATURE_THRESHOLD

# Cooldown after the breaker OPENs; a recurrence within the window is shed outright.
# Generous because a cross-turn defect is unlikely to self-resolve in seconds, and a
# human-driven code edit between turns produces a *different* signature anyway.
_COOLDOWN_S: float = 120.0

# Volatile fragments stripped so the same defect normalizes to one key.
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_LINE_NO_RE = re.compile(r"line \d+", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")
_MSG_CAP: int = 200


def normalize_signature(failed_node: str, exc_class: str, exc_message: str) -> str:
    """Collapse a failure into a stable cross-turn key.

    Line numbers, memory addresses and other bare integers are erased so the same
    defect produces an identical signature on every turn; the message is lowercased,
    whitespace-folded and capped to bound the key length.
    """
    msg = exc_message
    msg = _HEX_ADDR_RE.sub("0xADDR", msg)
    msg = _LINE_NO_RE.sub("line N", msg)
    msg = _DIGITS_RE.sub("N", msg)
    msg = _WS_RE.sub(" ", msg).strip().lower()[:_MSG_CAP]
    return f"{failed_node}\x00{exc_class}\x00{msg}"


class FailureSignatureBreaker:
    """Per-signature failure-streak gate. Mirrors core/indexer.py::_ReactiveBreaker."""

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._state: Dict[str, Tuple[int, float]] = {}  # signature -> (failures, opened_at)
        self._time = time_fn

    def allow(self, signature: str) -> bool:
        """True if a fresh reflexion attempt is permitted for this signature."""
        entry = self._state.get(signature)
        if entry is None:
            return True
        failures, opened_at = entry
        if failures < FAILURE_SIGNATURE_THRESHOLD:
            return True  # still CLOSED
        # OPEN: shed until the cooldown elapses, then permit one half-open trial.
        return (self._time() - opened_at) >= _COOLDOWN_S

    def record_success(self, signature: str) -> None:
        """A healed turn clears the signature so a future recurrence starts fresh."""
        self._state.pop(signature, None)

    def record_failure(self, signature: str) -> None:
        """Count a failed reflexion attempt; stamp the cooldown once OPEN so a failed
        half-open trial restarts the window instead of immediately re-permitting."""
        failures = self._state.get(signature, (0, 0.0))[0] + 1
        opened_at = self._time() if failures >= FAILURE_SIGNATURE_THRESHOLD else 0.0
        self._state[signature] = (failures, opened_at)


# Process-wide singleton — self-healing state must survive across graph invocations.
failure_breaker = FailureSignatureBreaker()
