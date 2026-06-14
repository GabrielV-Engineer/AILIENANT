"""Cognitive Quarantine boundary wrapper — shared across read-only tool bundles.

`wrap_boundary` is the single source of truth for tagging untrusted tool output
(extracted file content, parsed documents, web pages) with the per-turn boundary
tag so the model treats the material as inert data, never as instructions. Tools
inject a `boundary_provider` that reads the state-owned `boundary_id`; when none
is available the helper falls back to a locally-generated uuid4 with a logged
warning so a missing provider can never crash the tool.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

logger = logging.getLogger("QUARANTINE")


def wrap_boundary(text: str, boundary_provider: Optional[Callable[[], str]]) -> str:
    """Wrap untrusted content in the per-turn Cognitive Quarantine tag.

    Falls back to a locally-generated uuid4 with a logged warning when the
    state-owned boundary is unavailable — defensive only; production wires
    boundary_provider to read state["boundary_id"].
    """
    if boundary_provider is not None:
        try:
            tag = boundary_provider() or ""
        except Exception as exc:  # noqa: BLE001 — provider failures must not crash the tool
            logger.warning("boundary_provider raised %s; falling back to local uuid.", exc)
            tag = ""
    else:
        tag = ""
    if not tag:
        tag = uuid.uuid4().hex
        logger.debug("No state-owned boundary_id available; using local fallback.")
    return f"<{tag}>{text}</{tag}>"
