# ailienant-core/validators/environment.py
"""Phase 4.2.1 / 4.2.2 — Environment Introspection Engine + Pre-flight Check.

Probes the workspace for typing-config files (mypy.ini / pyproject.toml). When
none are found, sets ``relaxed_typing_mode=True`` so downstream linters can run
with ``--ignore-missing-imports`` instead of breaking on missing 3rd-party
stubs (graceful degradation).

No LLM, no tokens, zero VRAM. Pure file-system probing.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("VALIDATORS_ENVIRONMENT")


async def verify_environment_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve interpreter + probe for typing config. Pure file-system check.

    Interpreter resolution: explicit ``state["venv_interpreter_path"]`` overrides
    ``sys.executable``. Tests using the default fall-through prove the second.

    R10 caveat: ``pyproject.toml`` presence is treated as "typing config likely"
    even though the file exists in every modern Python project regardless of
    mypy setup. A future refinement should parse the ``[tool.mypy]`` section.
    """
    explicit = state.get("venv_interpreter_path")
    interpreter: str = (
        explicit if isinstance(explicit, str) and explicit else sys.executable
    )

    ws_root_str = state.get("workspace_root") or "."
    ws_root = Path(ws_root_str)
    has_mypy_ini = (ws_root / "mypy.ini").is_file()
    has_pyproject = (ws_root / "pyproject.toml").is_file()  # R10 — imprecise

    strict_available = has_mypy_ini or has_pyproject
    relaxed_typing_mode = not strict_available

    logger.info(
        "verify_environment: interpreter=%s mypy_ini=%s pyproject=%s "
        "→ relaxed_typing_mode=%s",
        interpreter,
        has_mypy_ini,
        has_pyproject,
        relaxed_typing_mode,
    )
    return {
        "venv_interpreter_path": interpreter,
        "relaxed_typing_mode": relaxed_typing_mode,
    }
