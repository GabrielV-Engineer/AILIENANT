#!/usr/bin/env python
"""Pre-commit entry point for the backend ruff/mypy hooks.

Resolves the project's own venv (mirrors ailienant-extension/e2e/run-backend.mjs's
PYTHON_CANDIDATES pattern) rather than hardcoding a platform-specific interpreter
path in .pre-commit-config.yaml, so the same hook works on Windows and POSIX
contributor machines and CI runners alike.

mypy.ini's explicit_package_bases/mypy_path only resolve correctly when mypy
runs with cwd=ailienant-core/ (flat sibling packages collide on basename
otherwise, per the ini's own header comment) — this filters staged files down
to that subtree, strips the prefix, and invokes the tool from there. This is a
fast local approximation of the full-tree CI gate, not a replacement for it.

Usage: pre_commit_backend_gate.py <ruff|mypy> <file> [<file> ...]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "ailienant-core"
_BIN_DIRS = (CORE_DIR / "venv", CORE_DIR / ".venv")


def _resolve(tool: str) -> Optional[Path]:
    for base in _BIN_DIRS:
        for candidate in (base / "Scripts" / f"{tool}.exe", base / "bin" / tool):
            if candidate.exists():
                return candidate
    return None


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: pre_commit_backend_gate.py <ruff|mypy> <file>...", file=sys.stderr)
        return 1
    tool, files = argv[0], argv[1:]

    changed = []
    for f in files:
        try:
            changed.append(Path(f).relative_to("ailienant-core").as_posix())
        except ValueError:
            continue
    if not changed:
        return 0

    binary = _resolve(tool)
    if binary is None:
        print(
            f"[pre-commit] No {tool} found under ailienant-core/venv or .venv — "
            "create the venv and install requirements.txt first. Skipping.",
            file=sys.stderr,
        )
        return 0

    args = [str(binary), "check", *changed] if tool == "ruff" else [str(binary), *changed]
    result = subprocess.run(args, cwd=CORE_DIR)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
