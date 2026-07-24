"""core/config/model_pricing.py

Best-effort per-model token pricing for the dashboard's BYOM cost badges.

The system deliberately hardcodes no pricing table (that would rot the moment a
provider changed rates). Instead we read litellm's bundled ``model_cost`` map —
already a dependency — and normalize our canonical ``provider/model`` ids onto
its keys. When a model isn't in the map we return ``None`` so the UI shows *no*
badge rather than a guessed number; local models are free by construction.

Honesty rules this module:
  - Local provider (``ProviderSpec.is_local``) → free, without consulting litellm.
  - Cloud model found in ``litellm.model_cost`` → real input/output rate.
  - Anything else → ``None`` (the caller renders nothing).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.config.provider_registry import get_provider, normalize_model_string

# Multiplier from litellm's per-token rate to a human-legible per-1M-tokens rate.
_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """Per-1M-token cost for a model. ``local`` models are free by construction."""

    input_per_mtok: float
    output_per_mtok: float
    local: bool


def _litellm_cost_map() -> Dict[str, Any]:
    """Return litellm's ``model_cost`` dict, or ``{}`` if litellm is unavailable.

    Deferred import — litellm is heavy and this runs on the config read path.
    """
    try:
        import litellm  # noqa: PLC0415 — deferred to keep module import light
    except Exception:  # noqa: BLE001 — a missing/broken litellm must not break config
        return {}
    cost = getattr(litellm, "model_cost", None)
    return cost if isinstance(cost, dict) else {}


def _lookup_candidates(model_id: str) -> List[str]:
    """Ordered litellm-key candidates for a canonical ``provider/model`` id.

    Most specific first: the litellm-routable string derived from the provider
    spec (e.g. ``google/gemini-2.0-flash`` → ``gemini/gemini-2.0-flash``), then
    the id verbatim, then the bare model stem (covers ``openai/gpt-4o`` → the
    bare ``gpt-4o`` key litellm actually stores).
    """
    candidates: List[str] = []
    head = model_id.split("/", 1)[0]
    spec = get_provider(head)
    if spec is not None:
        normalized = normalize_model_string(model_id, spec)
        if normalized not in candidates:
            candidates.append(normalized)
    if model_id not in candidates:
        candidates.append(model_id)
    if "/" in model_id:
        stem = model_id.split("/", 1)[1]
        if stem not in candidates:
            candidates.append(stem)
    return candidates


def price_for(model_id: str) -> Optional[ModelPrice]:
    """Resolve per-1M-token pricing for a canonical model id, or ``None``.

    Local models (resolved via the provider prefix) are free and never touch the
    litellm map. Cloud models are matched against ``litellm.model_cost`` through a
    small candidate ladder; an unmatched model yields ``None`` so the UI shows no
    badge instead of a fabricated figure.
    """
    head = model_id.split("/", 1)[0]
    spec = get_provider(head)
    if spec is not None and spec.is_local:
        return ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0, local=True)

    cost_map = _litellm_cost_map()
    if not cost_map:
        return None

    for key in _lookup_candidates(model_id):
        entry = cost_map.get(key)
        if not isinstance(entry, dict):
            continue
        inp = entry.get("input_cost_per_token")
        out = entry.get("output_cost_per_token")
        if inp is None and out is None:
            continue
        return ModelPrice(
            input_per_mtok=round(float(inp or 0.0) * _PER_MILLION, 4),
            output_per_mtok=round(float(out or 0.0) * _PER_MILLION, 4),
            local=False,
        )
    return None
