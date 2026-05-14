# ailienant-core/conftest.py
# Pytest root configuration — extends sys.path so that the flat-module
# subdirectories (agents/, api/) are importable by their short names.
# This mirrors the runtime environment where uvicorn runs from this directory
# and each subdirectory is on the PATH via PYTHONPATH or direct execution.

import sys
import os

_root = os.path.dirname(__file__)

# agents/ uses flat imports (e.g. `from prompts import build_safe_prompt`)
# which only work when the agents/ directory itself is on sys.path.
for _subdir in ("agents", "api"):
    _path = os.path.join(_root, _subdir)
    if _path not in sys.path:
        sys.path.insert(0, _path)
