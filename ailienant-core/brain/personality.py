# ailienant-core/brain/personality.py
"""Phase 4.1.5 — Soul (Persona) Manager for the AnalystAgent.

EXCLUSIVE CONSUMER: agents/analyst.py. Per blueprint §3.4 "Cognitive Isolation",
Planner, Coder, Orchestrator, and Researcher MUST NEVER import this module.
The fence is preserved by convention + this module-level warning. The test
suite (tests/test_analyst_agent.py::test_soul_manager_not_imported_by_logic_agents)
audits the four logic-agent source files for foreign imports on every CI run.

Reads SOUL.md (the persona configuration) with an mtime-based cache so the
operator can edit the file and see persona changes on the next analyst turn
without restarting the server. Falls back to a built-in 🐜 prompt when the
file is absent or misconfigured (default install, before the user customises
their persona).

Path resolution order:
  1. constructor ``path=`` argument (DI for tests)
  2. ``AILIENANT_SOUL_PATH`` env var
  3. ``~/.ailienant/SOUL.md`` (default)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from shared.persona import compose

logger = logging.getLogger("PERSONALITY_MANAGER")

_DEFAULT_SOUL_PROMPT: str = (
    "An empathetic Socratic copilot 🐜. "
    "Use analogies. Ask ONE guiding question per turn instead of writing code. "
    "Never mutate files; you are the Voice, not the Hand."
)


class SoulManager:
    """mtime-cached reader for ``~/.ailienant/SOUL.md``.

    Thread-unsafe by design — single-threaded LangGraph node access. If the
    Analyst ever fans out to parallel branches, wrap reads in a Lock.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._explicit_path: Optional[Path] = path
        self._cached_mtime: float = 0.0
        self._cached_content: str = ""

    def _resolve_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        env = os.environ.get("AILIENANT_SOUL_PATH")
        if env:
            return Path(env)
        return Path.home() / ".ailienant" / "SOUL.md"

    def get_prompt(self) -> str:
        """Return the persona prompt. Falls back to default when path is invalid.

        Hot-reload contract: if the file's mtime has advanced since the last
        read, the cache is invalidated and the file is re-read. Otherwise the
        cached content is returned with no disk I/O.

        Path-shape guard (R6): if the resolved path is NOT a regular file
        (missing, a directory, a symlink to a missing target), return the
        built-in fallback with a diagnostic log distinguishing the case.
        Prevents IsADirectoryError when AILIENANT_SOUL_PATH is misconfigured
        with a trailing slash.
        """
        path = self._resolve_path()

        # R6 — distinguish "missing" vs "is a directory" for operator debugging.
        if not path.is_file():
            if path.is_dir():
                logger.warning(
                    "SoulManager: configured path is a DIRECTORY, not a file: %s "
                    "(check AILIENANT_SOUL_PATH for a trailing slash). "
                    "Using built-in fallback persona.",
                    path,
                )
            else:
                logger.debug(
                    "SoulManager: path %s is absent. Using built-in fallback persona.",
                    path,
                )
            return compose(_DEFAULT_SOUL_PROMPT)

        try:
            mtime = path.stat().st_mtime
        except OSError as err:
            logger.warning("SoulManager: stat failed for %s: %s", path, err)
            return compose(_DEFAULT_SOUL_PROMPT)

        if mtime > self._cached_mtime:
            try:
                self._cached_content = path.read_text(encoding="utf-8")
                self._cached_mtime = mtime
                logger.info(
                    "SoulManager: refreshed cache from %s (%d chars, mtime=%.3f).",
                    path,
                    len(self._cached_content),
                    mtime,
                )
            except OSError as err:
                # Read failed despite is_file()+stat() succeeding — race or
                # permission flip. Return fallback without caching so a later
                # retry can recover.
                logger.warning("SoulManager: read failed for %s: %s", path, err)
                return compose(_DEFAULT_SOUL_PROMPT)

        return compose(self._cached_content or _DEFAULT_SOUL_PROMPT)


# Module-level singleton. The Analyst imports this directly:
#     from brain.personality import soul_manager
# DO NOT import this elsewhere. Cognitive isolation fence (blueprint §3.4).
soul_manager: SoulManager = SoulManager()
