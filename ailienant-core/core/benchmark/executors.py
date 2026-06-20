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
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Mapping, Optional, Protocol

from core.benchmark.hygiene import BenchmarkAbort

if TYPE_CHECKING:
    from core.benchmark.codegen import Language

_EVAL_DIR_NAME = ".benchmark_eval"
_ORACLE_ENTRY = "__oracle_main__.py"
_TS_UNSUPPORTED = (
    "[unsupported_runtime: TypeScript is not executable in the python sandbox image]"
)


class WorkspaceError(ValueError):
    """A patch path is unsafe or a workspace could not be materialized."""


def _safe_relative(rel: str) -> PurePosixPath:
    """Validate a patch-relative path *lexically*, before any disk I/O.

    The check is purely textual — it never touches the filesystem and never
    resolves symlinks (``Path.resolve`` would follow a link out of the jail and
    then validate the escaped target). A patch key is rejected when it is empty,
    absolute, carries a Windows drive letter, uses a backslash separator, or
    contains a ``..`` component. Because the workspace is a freshly created empty
    directory into which only regular files are written, a lexically-confined
    relative path cannot escape it.
    """
    if not rel or rel in (".", ".."):
        raise WorkspaceError(f"empty or dot patch path: {rel!r}")
    if "\\" in rel or ":" in rel:
        raise WorkspaceError(f"backslash or drive in patch path: {rel!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise WorkspaceError(f"path traversal in patch path: {rel!r}")
    return pure


def _materialize_workspace(
    root: Path, corpus_src: Path, patch: Mapping[str, str], test_body: str
) -> None:
    """Lay out ``root`` as a runnable workspace: corpus ``src`` + patch + entry.

    Copies the frozen corpus ``src`` tree, overlays each lexically-validated
    patch file, then writes the standalone test program as a root-level entry
    script. The script is run with ``root`` as the working directory, so Python
    places ``root`` on ``sys.path[0]`` and ``import src.…`` resolves with no
    embedded absolute path (identical on the host and inside the container).
    """
    shutil.copytree(str(corpus_src), str(root / "src"))
    for rel, content in patch.items():
        target = root / _safe_relative(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / _ORACLE_ENTRY).write_text(test_body, encoding="utf-8")


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

    async def run_workspace(
        self,
        *,
        corpus_src: Path,
        patch: Mapping[str, str],
        test_body: str,
        language: "Language",
        timeout_s: float,
    ) -> ExecOutcome:
        """Materialize a multi-file workspace and run its test program.

        Unlike :meth:`run` (one self-contained program), this lays out the
        frozen corpus ``src`` tree plus a candidate ``patch`` and executes
        ``test_body`` against them inside the executor's isolation envelope.
        """
        ...


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

    async def run_workspace(
        self,
        *,
        corpus_src: Path,
        patch: Mapping[str, str],
        test_body: str,
        language: "Language",
        timeout_s: float,
    ) -> ExecOutcome:
        if _is_typescript(language):
            return ExecOutcome(False, -2, "", _TS_UNSUPPORTED)

        from core.sandbox import DockerSandboxAdapter, get_active_adapter

        await self._ensure_docker_tier()
        adapter = get_active_adapter()
        # The Docker-tier guard guarantees the active adapter is the Docker one;
        # its mount root is the single authority for what the container can read.
        assert isinstance(adapter, DockerSandboxAdapter)

        eval_dir = Path(adapter.host_workspace) / _EVAL_DIR_NAME
        work_dir = eval_dir / f"oracle_{uuid.uuid4().hex}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            # Materialize inside a thread — copytree + writes are blocking I/O.
            await asyncio.to_thread(
                _materialize_workspace, work_dir, corpus_src, patch, test_body
            )
            # Run with the workspace as cwd: the adapter translates the host path
            # into ``/workspace/…`` and Python puts that dir on sys.path[0], so
            # ``import src.…`` resolves with no absolute path baked into the code.
            # PYTHONDONTWRITEBYTECODE stops the root container from leaving
            # root-owned ``__pycache__`` that the host could not later remove.
            result = await adapter.execute(
                f"python3 {_ORACLE_ENTRY}",
                timeout_s=timeout_s,
                cwd=str(work_dir),
                env_whitelist={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            return ExecOutcome(
                passed=result.exit_code == 0,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except WorkspaceError as exc:
            return ExecOutcome(False, -3, "", f"[unsafe_patch: {exc}]")
        finally:
            await asyncio.to_thread(shutil.rmtree, str(work_dir), True)


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

    async def run_workspace(
        self,
        *,
        corpus_src: Path,
        patch: Mapping[str, str],
        test_body: str,
        language: "Language",
        timeout_s: float,
    ) -> ExecOutcome:
        if _is_typescript(language):
            return ExecOutcome(False, -2, "", _TS_UNSUPPORTED)

        with tempfile.TemporaryDirectory() as _tmp:
            root = Path(_tmp)
            try:
                await asyncio.to_thread(
                    _materialize_workspace, root, corpus_src, patch, test_body
                )
            except WorkspaceError as exc:
                return ExecOutcome(False, -3, "", f"[unsafe_patch: {exc}]")

            # Run the entry script with the workspace as cwd so Python places it
            # on sys.path[0] (``import src.…`` resolves). The parent environment
            # is inherited (Windows needs SystemRoot/PATH to start the
            # interpreter); bytecode is disabled to keep the temp dir clean.
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                _ORACLE_ENTRY,
                cwd=str(root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                return ExecOutcome(False, -1, "", "[subprocess_timeout]")
            finally:
                # Always reap: fires on timeout and on cancellation.
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
