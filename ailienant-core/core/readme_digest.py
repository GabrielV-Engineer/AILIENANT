# core/readme_digest.py
"""Workspace-README orientation source for the analyst tutor.

The analyst injects the project's README so it can explain the repo the engineer
is working in. A large README must not be loaded whole into every prompt, so:

  * small READMEs (<= ``_SMALL_BYTES``) are used directly;
  * large ones are reduced to a **cached semantic digest** produced once by a
    small model in the background, keyed by the README's content hash;
  * until that digest exists, a **structure-aware head-slice** (headings + intro)
    is used — never a blind mid-content cut.

The background digest build is **debounced and cancellable**: a burst of saves
(compulsive Ctrl+S) collapses to a single build after a quiet window, so it never
spawns a storm of model calls. Read-only and best-effort: every entry point
degrades to ``None`` rather than raising, so context assembly can fall back to a
synthesized workspace overview.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Callable, Dict, Optional

logger = logging.getLogger("README_DIGEST")

_SMALL_BYTES: int = 5 * 1024        # <= this: inject the README verbatim
_HEAD_SLICE_CHARS: int = 3072       # structure-aware fallback budget
_DIGEST_MAX_TOKENS: int = 512
_DEBOUNCE_S: float = 7.0            # quiet window before a reactive rebuild

# content_hash -> digest text.
_digest_cache: Dict[str, str] = {}
# project_root -> pending debounced build task.
_pending: Dict[str, "asyncio.Task[None]"] = {}

Reader = Callable[[str], Optional[str]]


def _readme_path(project_root: str) -> str:
    return os.path.join(project_root, "README.md")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head_slice(md: str, budget: int = _HEAD_SLICE_CHARS) -> str:
    """Keep whole leading lines (headings + intro) up to ``budget`` — never a mid-line cut."""
    out: list[str] = []
    size = 0
    for line in md.splitlines():
        if size + len(line) + 1 > budget and out:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out).strip()


def get_readme_brain(project_root: str, read: Reader) -> Optional[str]:
    """Return the README orientation block, or ``None`` when there is no README.

    Small READMEs are returned verbatim; large ones return the cached digest if
    ready, otherwise a head-slice (and a debounced background digest build is
    scheduled). Never raises.
    """
    if not project_root:
        return None
    try:
        content = read(_readme_path(project_root))
    except Exception as exc:  # noqa: BLE001 — a read failure must never crash assembly
        logger.debug("README read failed (non-fatal): %s", exc)
        return None
    if not content or not content.strip():
        return None

    if len(content.encode("utf-8")) <= _SMALL_BYTES:
        return content.strip()

    cached = _digest_cache.get(_hash(content))
    if cached:
        return cached
    schedule_digest(project_root, read)        # warm the digest for next time
    return _head_slice(content)


def schedule_digest(project_root: str, read: Reader) -> None:
    """Debounced, cancellable background digest build for a large README."""
    if not project_root:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — no running loop
        return
    pending = _pending.get(project_root)
    if pending is not None and not pending.done():
        pending.cancel()  # reset the debounce timer
    _pending[project_root] = loop.create_task(_debounced_build(project_root, read))


async def _debounced_build(project_root: str, read: Reader) -> None:
    me = asyncio.current_task()
    try:
        await asyncio.sleep(_DEBOUNCE_S)       # cancelled here if a newer save lands
        try:
            content = read(_readme_path(project_root))
        except Exception:  # noqa: BLE001
            return
        if not content or len(content.encode("utf-8")) <= _SMALL_BYTES:
            return
        key = _hash(content)
        if key in _digest_cache:
            return
        digest = await _build_digest(content)
        if digest:
            _digest_cache[key] = digest
    except asyncio.CancelledError:
        raise
    finally:
        if _pending.get(project_root) is me:
            _pending.pop(project_root, None)


_DIGEST_SYSTEM_PROMPT: str = (
    "You are summarizing a software project's README so a teammate can quickly "
    "orient. Produce a dense plain-prose digest (<=300 words) covering: what the "
    "project is, its architecture/key components, how to install or run it, and "
    "anything notable. No preamble, no markdown headings — just the summary."
)


async def _build_digest(readme: str) -> Optional[str]:
    """One-shot small-model summary of a large README. Returns None on failure."""
    try:
        from tools.llm_gateway import LLMGateway        # deferred — avoids cycle
        from shared.config import MINI_JUDGE_MODEL
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": readme[:16000]},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.2,
            max_tokens=_DIGEST_MAX_TOKENS,
        )
        text = response.choices[0].message.content
        return str(text).strip() if text else None
    except Exception as exc:  # noqa: BLE001 — digest is best-effort
        logger.debug("README digest build failed (non-fatal): %s", exc)
        return None
