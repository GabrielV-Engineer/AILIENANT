"""Pluggable execution backends for the codegen scorer.

Two backends sit behind one :class:`CodegenExecutor` protocol:

* :class:`SandboxCodegenExecutor` — the default for *live* model output. It runs
  the generated program inside the isolated Docker sandbox tier, writing the
  source to a file under the read-only mount's host side and executing it by
  path (never injecting the source into the shell argv, which would be both an
  injection vector and an ``ARG_MAX`` hazard).
* :class:`SubprocessPythonExecutor` — a host subprocess for the hermetic test
  gate, where the input is trusted (canonical/known solutions). It guarantees
  the child is reaped on every path so a hung program cannot leak into CI.

TypeScript is accepted by the interface but not executable in the current
sandbox image (``python:3.13-slim`` ships no Node/tsc); the sandbox backend
reports that explicitly rather than failing opaquely.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol

from core.benchmark.hygiene import BenchmarkAbort

if TYPE_CHECKING:
    from core.benchmark.codegen import Language

_EVAL_DIR_NAME = ".benchmark_eval"
_TS_UNSUPPORTED = (
    "[unsupported_runtime: TypeScript is not executable in the python sandbox image]"
)


@dataclass
class ExecOutcome:
    """The outcome of running one assembled program."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str


class CodegenExecutor(Protocol):
    """Runs an assembled program and reports whether its tests passed."""

    async def run(
        self, program: str, language: "Language", timeout_s: float
    ) -> ExecOutcome: ...


def _is_typescript(language: "Language") -> bool:
    """True when the target is TypeScript (deferred import to avoid a cycle)."""
    from core.benchmark.codegen import Language

    return language is Language.TYPESCRIPT


class SandboxCodegenExecutor:
    """Run generated Python inside the Docker sandbox tier.

    ``host_workspace`` is the host directory the active adapter bind-mounts
    read-only at ``/workspace``; it defaults to the current working directory,
    which is where the resolver mounts. The program is written there (the host
    owns the file; the read-only mount only blocks *container* writes) and
    executed by relative path.
    """

    def __init__(self, host_workspace: Optional[str] = None) -> None:
        self._host_workspace = host_workspace or os.getcwd()
        self._resolved = False

    async def _ensure_docker_tier(self) -> None:
        """Resolve the sandbox tier once and require Docker.

        Wasm is pure-compute (no filesystem) and NativeHITL needs a human, so
        neither can run a test file non-interactively. A non-Docker tier aborts
        cleanly instead of silently mis-measuring.
        """
        from core.sandbox import get_active_tier, resolve_default_adapter

        if not self._resolved:
            await resolve_default_adapter()
            self._resolved = True
        tier = get_active_tier()
        if tier != "DOCKER":
            raise BenchmarkAbort(
                f"sandbox codegen requires the Docker tier (active tier: {tier})"
            )

    async def run(
        self, program: str, language: "Language", timeout_s: float
    ) -> ExecOutcome:
        # TS is rejected before any tier/Docker/file work so the path is hermetic.
        if _is_typescript(language):
            return ExecOutcome(False, -2, "", _TS_UNSUPPORTED)

        from core.sandbox import get_active_adapter

        await self._ensure_docker_tier()
        adapter = get_active_adapter()
        assert adapter is not None  # narrowed by the Docker-tier guard above

        eval_dir = Path(self._host_workspace) / _EVAL_DIR_NAME
        eval_dir.mkdir(parents=True, exist_ok=True)
        name = f"eval_{uuid.uuid4().hex}.py"
        host_path = eval_dir / name
        relative = f"{_EVAL_DIR_NAME}/{name}"
        try:
            host_path.write_text(program, encoding="utf-8")
            result = await adapter.execute(
                f"python3 {relative}",
                timeout_s=timeout_s,
                cwd=self._host_workspace,
                env_whitelist={},
            )
            return ExecOutcome(
                passed=result.exit_code == 0,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        finally:
            try:
                host_path.unlink()
            except OSError:
                pass


class SubprocessPythonExecutor:
    """Run trusted Python in a host subprocess, reaping the child on every path.

    For the hermetic gate only: the input is trusted (the gate injects
    canonical/known solutions), so the parent environment is inherited — passing
    an empty environment would strip ``SystemRoot``/``PATH`` and the interpreter
    would fail to start on Windows. Untrusted live model output belongs in
    :class:`SandboxCodegenExecutor`, which applies the sandbox's env isolation.
    """

    async def run(
        self, program: str, language: "Language", timeout_s: float
    ) -> ExecOutcome:
        if _is_typescript(language):
            return ExecOutcome(False, -2, "", _TS_UNSUPPORTED)

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=program.encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return ExecOutcome(False, -1, "", "[subprocess_timeout]")
        finally:
            # Always reap: fires on timeout and on cancellation, so no child can
            # outlive the call and accumulate on the CI host.
            if process.returncode is None:
                process.kill()
                await process.wait()

        exit_code = process.returncode if process.returncode is not None else -1
        return ExecOutcome(
            passed=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )
