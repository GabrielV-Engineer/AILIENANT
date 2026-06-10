# core/token_ledger.py
"""Phase 3.4.8 — Hybrid Cognitive Architecture token telemetry.

Thread-safe global ledger that tracks prompt + completion tokens per tier
(LOCAL vs CLOUD) so the engine can quantify how much it saves by keeping
generation/fixing on cheap local models vs escalating to cloud reasoning.

Exposed to the IDE via GET /api/v1/telemetry/tokens (see main.py).
"""
from __future__ import annotations

import threading
from typing import Dict

# Default USD per 1k tokens. Conservative defaults; env override deferred to a later phase.
_USD_PER_K_LOCAL: float = 0.001
_USD_PER_K_CLOUD: float = 0.030      # C_in  — input (prompt) token cost
_USD_PER_K_CLOUD_OUT: float = 0.150  # C_out — output (completion) token cost (~5× input)


class TokenLedger:
    """Thread-safe counters for prompt + completion tokens by tier."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._local_prompt: int = 0
        self._local_completion: int = 0
        self._cloud_prompt: int = 0
        self._cloud_completion: int = 0

    def record_local(self, prompt: int, completion: int) -> None:
        """Add a LOCAL-tier LLM call's usage to the ledger."""
        with self._lock:
            self._local_prompt += max(0, int(prompt))
            self._local_completion += max(0, int(completion))

    def record_cloud(self, prompt: int, completion: int) -> None:
        """Add a CLOUD-tier LLM call's usage to the ledger."""
        with self._lock:
            self._cloud_prompt += max(0, int(prompt))
            self._cloud_completion += max(0, int(completion))

    def snapshot(self) -> Dict[str, float]:
        """Return the current counters + estimated savings.

        Savings heuristic: if every local token had instead been a cloud token,
        the marginal extra cost would have been (local_total * (cloud_rate - local_rate)).
        Inverse for invested: what we actually spent on cloud calls.
        """
        with self._lock:
            local_total: int = self._local_prompt + self._local_completion
            cloud_total: int = self._cloud_prompt + self._cloud_completion
        saved_usd: float = (local_total / 1000.0) * (_USD_PER_K_CLOUD - _USD_PER_K_LOCAL)
        invested_usd: float = (cloud_total / 1000.0) * _USD_PER_K_CLOUD
        return {
            "local_tokens": float(local_total),
            "cloud_tokens": float(cloud_total),
            "estimated_savings_usd": saved_usd,
            "estimated_invested_usd": invested_usd,
        }

    def reset(self) -> None:
        """Test-only helper: zero all counters."""
        with self._lock:
            self._local_prompt = 0
            self._local_completion = 0
            self._cloud_prompt = 0
            self._cloud_completion = 0


token_ledger: TokenLedger = TokenLedger()
