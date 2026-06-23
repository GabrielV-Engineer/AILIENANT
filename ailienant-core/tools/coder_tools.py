"""Role-specific coder arsenal — thin, RBAC-gated wrappers over existing engines.

Each tool here is a typed schema + a role gate. Execution is delegated to engines
that already exist: the process-global sandbox adapter (core.sandbox), the AST
validator (tools.validation.ast_filter), and the transactional patch validator
(tools.patch_tool). Nothing in this module re-implements sandbox dispatch, the
dangerous-command interceptor, or the token ledger.

Security model (Zero-Trust Bash):
  * Every path/argument that reaches a shell template passes ``_safe_arg`` first,
    which rejects flag injection ("-" prefix), path-traversal ("../"), and absolute
    paths, then ``shlex.quote``s the survivor. The ``--`` end-of-options separator is
    an additional layer for GNU-getopt CLIs (git, ruff, pytest) only; it is NOT relied
    upon for python/pip, where the argument guard plus quoting is the protection.
  * Validation failures RETURN a structured "[tool] REJECTED: ..." string and never
    arm a command — they do not raise, so a caller fault cannot crash the host.

allowed_roles mirror agents/roles.py: each net-new tool is gated to its exclusive
owning role(s); the AST validator is open to every code-producing role.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import shlex
from pathlib import PurePosixPath
from typing import (
    Any,
    Callable,
    FrozenSet,
    List,
    MutableMapping,
    NamedTuple,
    Optional,
    Tuple,
    Type,
)

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.exceptions import PatchError
from core.permissions import ToolPrivilegeTier
from core.sandbox import get_active_adapter
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.execution_tools import (
    _SANDBOX_UNINITIALIZED_MSG,
    _sandbox_env,
    _truncate,
)
from tools.patch_tool import _validate_python_syntax
from tools.validation.ast_filter import validate_ast

logger = logging.getLogger("CODER_TOOLS")


# =====================================================================
# Shared constants — role sets (mirror agents/roles.py) + timeouts
# =====================================================================

# Roles whose roles.py whitelist holds ``apply_patch`` (every role except vcs_manager).
# Used by the AST validator, which any code-producing role may call.
_APPLY_PATCH_ROLES: FrozenSet[str] = frozenset(
    {
        "core_dev",
        "architect_refactor",
        "devops_infra",
        "secops",
        "qa_tester",
        "doc_manager",
        "data_ml_engineer",
    }
)

_QA_ROLES: FrozenSet[str] = frozenset({"qa_tester"})
_VCS_ROLES: FrozenSet[str] = frozenset({"vcs_manager"})
_DOC_ROLES: FrozenSet[str] = frozenset({"doc_manager"})
_DEVOPS_ROLES: FrozenSet[str] = frozenset({"devops_infra"})
_DATA_ROLES: FrozenSet[str] = frozenset({"data_ml_engineer"})
_SECOPS_ROLES: FrozenSet[str] = frozenset({"secops"})
_LINTER_ROLES: FrozenSet[str] = frozenset({"secops", "qa_tester"})

_DEFAULT_TIMEOUT_SEC: float = 60.0
_TEST_TIMEOUT_SEC: float = 300.0
_INSTALL_TIMEOUT_SEC: float = 300.0
_PIPELINE_TIMEOUT_SEC: float = 600.0

# Conventional Commit type allowlist (git_commit composes, never parses).
_CONVENTIONAL_TYPES: FrozenSet[str] = frozenset(
    {"feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert"}
)
_SCOPE_RE = re.compile(r"^[a-z0-9\-]+$")

# Supply-chain lock: first char alphanumeric so a leading '-' can never slip in.
_PKG_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$")
_PKG_VERSION_RE = re.compile(r"^[0-9][a-zA-Z0-9_\-\.]*$")

# Windows drive-letter absolute path, e.g. C:\ or D:/ .
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# security_audit caps (token hygiene).
_AUDIT_INPUT_CAP: int = 20_000
_MAX_FINDINGS: int = 25


# =====================================================================
# Zero-Trust argument guard + sandbox dispatch
# =====================================================================


class _ArgRejected(Exception):
    """Raised by ``_safe_arg`` when an argument fails a Zero-Trust check.

    Always caught at the top of the owning tool's ``_arun`` and converted to a
    structured rejection string — it never escapes to the host.
    """


def _safe_arg(tok: str) -> str:
    """Validate a single shell argument, then return it ``shlex.quote``d.

    Rejects flag injection (leading '-'), path traversal ('../', '..\\'), and
    absolute paths (POSIX '/'/'\\' prefix or a Windows drive). All arguments must
    be project-root-relative; this is the floor that ``shlex.quote`` alone cannot
    provide (quoting stops subshells, not flags or traversal).
    """
    if tok.startswith("-"):
        raise _ArgRejected(f"argument {tok!r} starts with '-' (flag injection blocked)")
    if "../" in tok or "..\\" in tok:
        raise _ArgRejected(f"argument {tok!r} contains a path-traversal sequence")
    if tok.startswith("/") or tok.startswith("\\") or _WIN_DRIVE_RE.match(tok):
        raise _ArgRejected(f"argument {tok!r} is an absolute path; use a project-relative path")
    return shlex.quote(tok)


async def _exec(command: str, timeout_s: float) -> Tuple[int, str]:
    """Dispatch ``command`` through the active sandbox tier; truncate the output.

    Mirrors execution_tools.CheckTypeIntegrityTool: the adapter owns the timeout
    and the env whitelist (host secrets never leak). An unresolved adapter raises
    the same RuntimeError the execution tools use, never a silent no-op.
    """
    adapter = get_active_adapter()
    if adapter is None:
        raise RuntimeError(_SANDBOX_UNINITIALIZED_MSG)
    result = await adapter.execute(
        command,
        timeout_s=timeout_s,
        cwd="",
        env_whitelist=_sandbox_env(),
    )
    return result.exit_code, _truncate(result.stdout + result.stderr)


# =====================================================================
# Per-session HITL gate for EXECUTE-tier dispatch
# =====================================================================

# Human-in-the-loop approval ceiling for one gated command, in seconds. Mirrors
# the MCP adapter's HITL timeout so both interactive command gates feel identical.
_HITL_APPROVAL_TIMEOUT_SEC: float = 120.0


class _SessionCtx(NamedTuple):
    """Per-session execution context injected by the coder-tool factory.

    Carries the identity + permission policy needed to route an EXECUTE-tier
    command through the interactive HITL approval card, mirroring the gate the MCP
    adapter applies. It travels explicitly on the tool instance — never through an
    ambient ContextVar.
    """

    session_id: str
    permission_mode: str


async def _gated_exec(
    command: str,
    timeout_s: float,
    *,
    tool_name: str,
    tier: ToolPrivilegeTier,
    session_ctx: Optional[_SessionCtx],
) -> Tuple[int, str]:
    """Dispatch through the sandbox, optionally behind a per-session HITL gate.

    With ``session_ctx`` present the command is admitted by the permission matrix
    under the session's policy: DENY short-circuits (plan mode is read-only), HITL
    routes through the interactive approval card before anything runs, and
    ALLOW/already-trusted dispatches straight through. The trust-once valve skips a
    re-prompt for a tool the operator approved earlier in the same session.

    Without ``session_ctx`` the behavior is identical to :func:`_exec` — no gate,
    zero change for existing callers.
    """
    if session_ctx is not None:
        # Lazy local imports — avoid the api-layer import cycle (mirrors mcp_adapter).
        from core.permissions import (
            PermissionDecision,
            PermissionMode,
            evaluate_action,
            risk_intercept_guard,
            session_mode_from_channel,
        )
        from tools.mcp_adapter import _grant_session_trust, _is_session_trusted

        sid = session_ctx.session_id
        if not (sid and _is_session_trusted(sid, tool_name)):
            _sess_mode = session_mode_from_channel(session_ctx.permission_mode)
            verdict = evaluate_action(
                _sess_mode,
                tier,
                PermissionMode.EDIT_EXECUTE_RBW,
            )
            # YOLO Guard: upgrade ALLOW -> HITL for high-risk commands in permissive modes.
            verdict, _risk_labels = risk_intercept_guard(command[:2000], verdict, _sess_mode)
            if verdict is PermissionDecision.DENY:
                return -1, f"[{tool_name}] DENIED — plan mode is read-only; command not run."
            if verdict is PermissionDecision.HITL:
                if not sid:
                    return (
                        -1,
                        f"[{tool_name}] BLOCKED — approval required but no session is "
                        "available to route the request; command not run.",
                    )
                from api.websocket_manager import vfs_manager  # lazy — no cycle

                _kind = "RISK_INTERCEPT" if _risk_labels else "COMMAND_EXEC"
                approval = await vfs_manager.request_human_approval(
                    session_id=sid,
                    action_description=f"COMMAND_EXEC: {tool_name}",
                    proposed_content=command[:2000],
                    request_kind=_kind,
                    timeout_s=_HITL_APPROVAL_TIMEOUT_SEC,
                    risk_patterns_matched=_risk_labels or None,
                )
                if not approval or not approval.get("approved"):
                    return (
                        -1,
                        f"[{tool_name}] BLOCKED — command was not approved; command not run.",
                    )
                _grant_session_trust(sid, tool_name)

    return await _exec(command, timeout_s)


class _GatedExecTool(BaseTool):
    """Base for EXECUTE-tier coder tools whose dispatch may be HITL-gated.

    The factory injects a :class:`_SessionCtx` onto ``_session_ctx``; when present,
    :meth:`_gated_dispatch` routes the command through the per-session approval gate.
    When absent (the default — the schema-registration path) dispatch is the plain
    sandbox call, so unfactoried construction is unchanged.
    """

    _session_ctx: Optional[_SessionCtx] = PrivateAttr(default=None)

    async def _gated_dispatch(
        self,
        command: str,
        timeout_s: float,
        tier: ToolPrivilegeTier = ToolPrivilegeTier.EXECUTE,
    ) -> Tuple[int, str]:
        return await _gated_exec(
            command,
            timeout_s,
            tool_name=self.name,
            tier=tier,
            session_ctx=self._session_ctx,
        )


# =====================================================================
# qa_tester — RunTestsTool
# =====================================================================


class RunTestsInput(BaseModel):
    target: str = Field(default=".", description="Project-relative path or pytest node id to run.")


class RunTestsTool(_GatedExecTool):
    """Run pytest against a project-relative target inside the sandbox."""

    name: str = "run_tests"
    description: str = (
        "Run the pytest suite against a project-relative target (path or node id). "
        "Output is truncated to 2000 chars. Exclusive to the qa_tester role."
    )
    args_schema: Type[BaseModel] = RunTestsInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("RunTestsTool is async-only — use _arun().")

    async def _arun(self, target: str = ".") -> str:
        try:
            safe = _safe_arg(target)
        except _ArgRejected as exc:
            return f"[run_tests] REJECTED: {exc}"
        code, body = await self._gated_dispatch(f"pytest -q -- {safe}", _TEST_TIMEOUT_SEC)
        return f"[run_tests] exit={code}\n{body}"


# =====================================================================
# vcs_manager — GitStageTool / GitCommitTool / GitDiffTool
# =====================================================================


class GitStageInput(BaseModel):
    paths: List[str] = Field(description="Project-relative paths to stage.")


class GitStageTool(_GatedExecTool):
    """Stage project-relative paths with ``git add``."""

    name: str = "git_stage"
    description: str = (
        "Stage one or more project-relative paths for commit (git add). "
        "Exclusive to the vcs_manager role."
    )
    args_schema: Type[BaseModel] = GitStageInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GitStageTool is async-only — use _arun().")

    async def _arun(self, paths: List[str]) -> str:
        if not paths:
            return "[git_stage] REJECTED: no paths provided"
        try:
            safe = " ".join(_safe_arg(p) for p in paths)
        except _ArgRejected as exc:
            return f"[git_stage] REJECTED: {exc}"
        code, body = await self._gated_dispatch(f"git add -- {safe}", _DEFAULT_TIMEOUT_SEC)
        return f"[git_stage] exit={code}\n{body}"


class GitCommitInput(BaseModel):
    commit_type: str = Field(description="Conventional Commit type, e.g. feat, fix, docs.")
    subject: str = Field(description="Imperative commit subject (non-empty).")
    scope: Optional[str] = Field(default=None, description="Optional scope, e.g. 'core'.")


class GitCommitTool(_GatedExecTool):
    """Commit staged changes with a composed Conventional-Commit message."""

    name: str = "git_commit"
    description: str = (
        "Create a commit from validated parts (type, optional scope, subject). The "
        "message is composed in Conventional-Commit format; invalid parts are rejected "
        "before anything runs. Exclusive to the vcs_manager role."
    )
    args_schema: Type[BaseModel] = GitCommitInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GitCommitTool is async-only — use _arun().")

    async def _arun(self, commit_type: str, subject: str, scope: Optional[str] = None) -> str:
        if commit_type not in _CONVENTIONAL_TYPES:
            return (
                f"[git_commit] REJECTED: invalid type {commit_type!r}; "
                f"must be one of {sorted(_CONVENTIONAL_TYPES)}"
            )
        if not subject.strip():
            return "[git_commit] REJECTED: empty subject"
        if scope is not None and not _SCOPE_RE.match(scope):
            return f"[git_commit] REJECTED: invalid scope {scope!r} (must match {_SCOPE_RE.pattern})"
        # Conditional composition — an omitted scope must never yield "feat(): ...".
        composed = f"{commit_type}({scope}): {subject}" if scope else f"{commit_type}: {subject}"
        code, body = await self._gated_dispatch(
            f"git commit -m {shlex.quote(composed)}", _DEFAULT_TIMEOUT_SEC
        )
        return f"[git_commit] exit={code}\n{body}"


class GitDiffInput(BaseModel):
    staged: bool = Field(default=False, description="Diff the staged index instead of the worktree.")
    paths: List[str] = Field(default_factory=list, description="Optional project-relative paths.")


class GitDiffTool(_GatedExecTool):
    """Show an on-disk git diff (worktree or staged)."""

    name: str = "git_diff"
    description: str = (
        "Show a git diff of the ON-DISK repository state (worktree or, with staged=True, "
        "the index). NOTE: this reads on-disk git state. It does NOT show pending unapplied "
        "edits currently in your RAM VFS — use diff_changes (CodeDiffTool) for pending RAM "
        "changes. Output is truncated. Exclusive to the vcs_manager role."
    )
    args_schema: Type[BaseModel] = GitDiffInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GitDiffTool is async-only — use _arun().")

    async def _arun(self, staged: bool = False, paths: Optional[List[str]] = None) -> str:
        paths = paths or []
        try:
            safe_paths = [_safe_arg(p) for p in paths]
        except _ArgRejected as exc:
            return f"[git_diff] REJECTED: {exc}"
        parts: List[str] = ["git", "diff"]
        if staged:
            parts.append("--staged")
        if safe_paths:
            parts.append("--")
            parts.extend(safe_paths)
        code, body = await self._gated_dispatch(" ".join(parts), _DEFAULT_TIMEOUT_SEC)
        return f"[git_diff] exit={code}\n{body}"


# =====================================================================
# doc_manager — DocstringGeneratorTool
# =====================================================================


class GenerateDocstringInput(BaseModel):
    file_path: str = Field(description="VFS path of the file to document.")
    symbol_name: str = Field(description="The function or class name to add a docstring to.")


class DocstringGeneratorTool(BaseTool):
    """Insert a placeholder docstring into a function/class via AST anchoring.

    Reads the file from the VFS, locates the target symbol with ``ast.parse``, and
    inserts an indented stub as the symbol's first body statement. A pre-existing
    SyntaxError (or pathological nesting that trips RecursionError) returns a
    structured error rather than crashing the node.
    """

    name: str = "generate_docstring"
    description: str = (
        "Add a placeholder docstring to a named function or class in a VFS file. "
        "The new content is AST-validated before it is written. Exclusive to the "
        "doc_manager role."
    )
    args_schema: Type[BaseModel] = GenerateDocstringInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _vfs_read: Callable[[str], Optional[str]] = PrivateAttr()
    _vfs_write: Callable[[str, str], None] = PrivateAttr()

    def __init__(
        self,
        *,
        vfs_read: Callable[[str], Optional[str]],
        vfs_write: Callable[[str, str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vfs_read = vfs_read
        self._vfs_write = vfs_write

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DocstringGeneratorTool is async-only — use _arun().")

    async def _arun(self, file_path: str, symbol_name: str) -> str:
        content = self._vfs_read(file_path)
        if content is None:
            return f"[generate_docstring] ERROR: file {file_path!r} not found in VFS"

        try:
            tree = ast.parse(content)
        except (SyntaxError, RecursionError):
            return json.dumps({"error": "Fix syntax errors before generating docstrings."})

        target: Optional[ast.AST] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == symbol_name
            ):
                target = node
                break
        if target is None:
            return f"[generate_docstring] ERROR: symbol {symbol_name!r} not found in {file_path!r}"

        # mypy: the isinstance above narrows; re-assert for the body access below.
        assert isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if ast.get_docstring(target) is not None:
            return f"[generate_docstring] SKIP: {symbol_name!r} already has a docstring"
        if not target.body:
            return f"[generate_docstring] ERROR: {symbol_name!r} has an empty body"

        first_stmt = target.body[0]
        # A single-line definition ("def f(): return 1") shares the header line; a
        # line-anchored insert cannot safely split it.
        if first_stmt.lineno == target.lineno:
            return f"[generate_docstring] SKIP: {symbol_name!r} is single-line; cannot anchor a docstring"

        lines = content.splitlines()
        indent = " " * first_stmt.col_offset
        doc_line = f'{indent}"""TODO: document {symbol_name}."""'
        insert_at = first_stmt.lineno - 1  # 0-based index of the first body statement
        new_lines = lines[:insert_at] + [doc_line] + lines[insert_at:]
        new_content = "\n".join(new_lines)
        if content.endswith("\n"):
            new_content += "\n"

        try:
            _validate_python_syntax(new_content, file_path)
        except PatchError as exc:
            return f"[generate_docstring] ERROR: {exc}"

        self._vfs_write(file_path, new_content)
        return f"[generate_docstring] OK: docstring added to {symbol_name!r} in {file_path!r}"


# =====================================================================
# secops / qa_tester — LinterAutoFixTool
# =====================================================================


class LinterAutoFixInput(BaseModel):
    target: str = Field(description="Project-relative path to lint.")
    apply: bool = Field(
        default=False,
        description="False (default) shows the fix as a diff; True applies it in place.",
    )


class LinterAutoFixTool(_GatedExecTool):
    """Run ruff over a target; show the fix as a diff or apply it in place."""

    name: str = "linter_autofix"
    description: str = (
        "Lint a project-relative target with ruff. With apply=False (default) the "
        "proposed fix is shown as a diff; with apply=True the fix is written in place. "
        "Available to the secops and qa_tester roles."
    )
    args_schema: Type[BaseModel] = LinterAutoFixInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("LinterAutoFixTool is async-only — use _arun().")

    async def _arun(self, target: str, apply: bool = False) -> str:
        try:
            safe = _safe_arg(target)
        except _ArgRejected as exc:
            return f"[linter_autofix] REJECTED: {exc}"
        mode = "--fix" if apply else "--diff"
        code, body = await self._gated_dispatch(
            f"ruff check {mode} -- {safe}", _DEFAULT_TIMEOUT_SEC
        )
        return f"[linter_autofix] exit={code}\n{body}"


# =====================================================================
# devops_infra — DependencyInstallTool / EnvFileGuardTool
# =====================================================================


class DependencyInstallInput(BaseModel):
    name: str = Field(description="PyPI package name (alphanumeric start; no URLs or VCS refs).")
    version: Optional[str] = Field(default=None, description="Optional exact version, e.g. '1.2.3'.")


class DependencyInstallTool(_GatedExecTool):
    """Install a single PyPI package (optionally pinned) inside the sandbox."""

    name: str = "install_dependency"
    description: str = (
        "Install one package from the PyPI index via pip, optionally pinned to an exact "
        "version. The name/version are strictly validated; URLs, VCS refs, and editable "
        "installs are rejected. Exclusive to the devops_infra role."
    )
    args_schema: Type[BaseModel] = DependencyInstallInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DependencyInstallTool is async-only — use _arun().")

    async def _arun(self, name: str, version: Optional[str] = None) -> str:
        if not _PKG_NAME_RE.match(name):
            return f"[install_dependency] REJECTED: invalid package name {name!r}"
        if version is not None and not _PKG_VERSION_RE.match(version):
            return f"[install_dependency] REJECTED: invalid version {version!r}"
        spec = f"{name}=={version}" if version else name
        # No "--" for pip (it does not honor end-of-options like git); the strict
        # regex + shlex.quote is the protection.
        code, body = await self._gated_dispatch(
            f"python -m pip install {shlex.quote(spec)}", _INSTALL_TIMEOUT_SEC
        )
        return f"[install_dependency] exit={code}\n{body}"


def _env_file_name(path: str) -> str:
    """Return the lowercased basename, tolerant of POSIX and Windows separators."""
    return PurePosixPath(path.replace("\\", "/")).name.lower()


class GuardEnvFileInput(BaseModel):
    file_path: str = Field(description="Path of the file the agent intends to mutate.")
    proposed_content: str = Field(description="The content the agent intends to write.")


class GuardEnvFileTool(BaseTool):
    """Intercept ``.env`` mutations and force a human-in-the-loop gate.

    A mutation targeting an environment file is never written by this tool; instead
    it emits a stable, content-derived HITL gate id (same path + content always
    yields the same id, so accidental retries are idempotent). Non-env paths pass
    through untouched.
    """

    name: str = "guard_env_file"
    description: str = (
        "Guard environment files (.env, .env.*): a mutation targeting one is intercepted "
        "and routed to a human-in-the-loop approval gate instead of being written. "
        "Exclusive to the devops_infra role."
    )
    args_schema: Type[BaseModel] = GuardEnvFileInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GuardEnvFileTool is async-only — use _arun().")

    async def _arun(self, file_path: str, proposed_content: str) -> str:
        name = _env_file_name(file_path)
        if not (name == ".env" or name.startswith(".env.")):
            return f"[guard_env_file] PASS: {file_path!r} is not an environment file; no guard needed"
        request_id = hashlib.sha256(
            f"{file_path}\x00{proposed_content}".encode("utf-8")
        ).hexdigest()[:16]
        logger.warning(
            "guard_env_file: intercepted environment-file mutation path=%s gate=%s",
            file_path,
            request_id,
        )
        return f"[guard_env_file] HITL_GATE:{request_id}"


# =====================================================================
# data_ml_engineer — DataPipelineRunTool
# =====================================================================


class RunDataPipelineInput(BaseModel):
    pipeline_path: str = Field(description="Project-relative path to the pipeline script.")


class DataPipelineRunTool(_GatedExecTool):
    """Execute a project-relative data-pipeline script inside the sandbox."""

    name: str = "run_data_pipeline"
    description: str = (
        "Run a project-relative Python data-pipeline script inside the sandbox. Output is "
        "truncated. Exclusive to the data_ml_engineer role."
    )
    args_schema: Type[BaseModel] = RunDataPipelineInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("DataPipelineRunTool is async-only — use _arun().")

    async def _arun(self, pipeline_path: str) -> str:
        try:
            safe = _safe_arg(pipeline_path)
        except _ArgRejected as exc:
            return f"[run_data_pipeline] REJECTED: {exc}"
        # No "--" for python; the argument guard + shlex.quote is the protection.
        code, body = await self._gated_dispatch(f"python {safe}", _PIPELINE_TIMEOUT_SEC)
        return f"[run_data_pipeline] exit={code}\n{body}"


# =====================================================================
# secops — SecurityAuditTool
# =====================================================================

# Simple, non-backtracking OWASP heuristics scanned over a supplied diff/code string.
_OWASP_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    (
        "hardcoded_secret",
        re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*=\s*['\"][^'\"]{6,}"),
    ),
    ("dangerous_eval_exec", re.compile(r"\b(eval|exec)\s*\(")),
    ("subprocess_shell_true", re.compile(r"shell\s*=\s*True")),
    ("insecure_pickle", re.compile(r"\bpickle\.loads?\s*\(")),
    ("unsafe_yaml_load", re.compile(r"\byaml\.load\s*\(")),
    ("dynamic_sql", re.compile(r"(?i)\b(?:execute|executemany)\s*\(\s*f?['\"]")),
]


class SecurityAuditInput(BaseModel):
    diff: str = Field(description="Code or unified diff to audit for OWASP-style risks.")


class SecurityAuditTool(BaseTool):
    """Scan a diff/code string for common OWASP patterns (pure-Python, no spawn)."""

    name: str = "security_audit"
    description: str = (
        "Audit a code or unified-diff string for common OWASP risks: hardcoded secrets, "
        "eval/exec, subprocess shell=True, insecure pickle, unsafe yaml.load, and dynamic "
        "SQL. Returns a capped findings list. Exclusive to the secops role."
    )
    args_schema: Type[BaseModel] = SecurityAuditInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("SecurityAuditTool is async-only — use _arun().")

    async def _arun(self, diff: str) -> str:
        text = diff[:_AUDIT_INPUT_CAP]
        findings: List[dict[str, str]] = []
        for label, pattern in _OWASP_PATTERNS:
            for match in pattern.finditer(text):
                findings.append({"issue": label, "match": match.group(0)[:80]})
                if len(findings) >= _MAX_FINDINGS:
                    break
            if len(findings) >= _MAX_FINDINGS:
                break
        return json.dumps(
            {
                "findings": findings,
                "scanned_chars": len(text),
                "clean": not findings,
            }
        )


# =====================================================================
# Formalize — ASTValidateTool (wraps validate_ast)
# =====================================================================


class ASTValidateInput(BaseModel):
    file_path: str = Field(description="Path used to select the language (extension).")
    content: str = Field(description="Source content to validate structurally.")


class ASTValidateTool(BaseTool):
    """Structurally validate source content via the shared AST filter (no spawn)."""

    name: str = "validate_ast"
    description: str = (
        "Structurally validate source content (Python via ast.parse, TS/TSX via the AST "
        "engine; other extensions pass through). Returns {is_valid, errors}. Available to "
        "every code-producing role."
    )
    args_schema: Type[BaseModel] = ASTValidateInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ASTValidateTool is async-only — use _arun().")

    async def _arun(self, file_path: str, content: str) -> str:
        result = validate_ast(content, file_path)
        errors = [
            {
                "layer": getattr(err, "layer", ""),
                "line": getattr(err, "line", None),
                "column": getattr(err, "column", None),
                "message": getattr(err, "message", ""),
            }
            for err in getattr(result, "errors", [])
        ]
        return json.dumps({"is_valid": result.is_valid, "errors": errors})


# =====================================================================
# Schema registration helper + register_coder_tools
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    json_schema_class: Type[BaseModel],
    *,
    tier: ToolPrivilegeTier,
    roles: FrozenSet[str],
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=tier,
        allowed_roles=roles,
    )


async def register_coder_tools(store: ToolRAGStore) -> int:
    """Register the 11 role-specific coder schemas in the given store. Returns count."""
    _ro = ToolPrivilegeTier.READ_ONLY
    _wr = ToolPrivilegeTier.WRITE
    _ex = ToolPrivilegeTier.EXECUTE
    _dg = ToolPrivilegeTier.DANGEROUS

    schemas: List[ToolSchema] = [
        _tool_schema(
            "run_tests",
            "Run pytest against a project-relative target inside the sandbox.",
            RunTestsInput,
            tier=_ex,
            roles=_QA_ROLES,
        ),
        _tool_schema(
            "git_stage",
            "Stage project-relative paths for commit (git add).",
            GitStageInput,
            tier=_ex,
            roles=_VCS_ROLES,
        ),
        _tool_schema(
            "git_commit",
            "Commit staged changes with a composed Conventional-Commit message.",
            GitCommitInput,
            tier=_ex,
            roles=_VCS_ROLES,
        ),
        _tool_schema(
            "git_diff",
            "Show an on-disk git diff (worktree or staged); not RAM-VFS pending edits.",
            GitDiffInput,
            tier=_ex,
            roles=_VCS_ROLES,
        ),
        _tool_schema(
            "generate_docstring",
            "Insert a placeholder docstring into a function or class via AST anchoring.",
            GenerateDocstringInput,
            tier=_wr,
            roles=_DOC_ROLES,
        ),
        _tool_schema(
            "linter_autofix",
            "Lint a target with ruff; show the fix as a diff or apply it in place.",
            LinterAutoFixInput,
            tier=_ex,
            roles=_LINTER_ROLES,
        ),
        _tool_schema(
            "install_dependency",
            "Install one strictly-validated PyPI package (optionally pinned) via pip.",
            DependencyInstallInput,
            tier=_ex,
            roles=_DEVOPS_ROLES,
        ),
        _tool_schema(
            "guard_env_file",
            "Intercept environment-file mutations and route them to a HITL approval gate.",
            GuardEnvFileInput,
            tier=_dg,
            roles=_DEVOPS_ROLES,
        ),
        _tool_schema(
            "run_data_pipeline",
            "Execute a project-relative data-pipeline script inside the sandbox.",
            RunDataPipelineInput,
            tier=_ex,
            roles=_DATA_ROLES,
        ),
        _tool_schema(
            "security_audit",
            "Scan a diff or code string for common OWASP risks (pure-Python, no spawn).",
            SecurityAuditInput,
            tier=_ro,
            roles=_SECOPS_ROLES,
        ),
        _tool_schema(
            "validate_ast",
            "Structurally validate source content via the shared AST filter.",
            ASTValidateInput,
            tier=_ro,
            roles=_APPLY_PATCH_ROLES,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)


def make_coder_execute_tools(state: MutableMapping[str, Any]) -> List[BaseTool]:
    """Construct the EXECUTE-tier coder tools bound to the session's HITL context.

    Reads ``session_id`` + ``session_permission_mode`` from the live graph state and
    injects them so each tool routes its command through the interactive approval
    card under the session's policy (mirroring the sandbox_bash gate). The session
    context travels explicitly on the instance — no ambient ContextVar.

    ``guard_env_file`` is intentionally excluded: it already emits its own
    content-hash-idempotent HITL gate and must not be double-gated. The read-only /
    pure-Python tools (security_audit, validate_ast) and the WRITE-tier
    generate_docstring need no command gate and are not built here.
    """
    ctx = _SessionCtx(
        session_id=str(state.get("session_id") or ""),
        permission_mode=str(state.get("session_permission_mode") or "DEFAULT"),
    )
    tools: List[BaseTool] = [
        RunTestsTool(),
        GitStageTool(),
        GitCommitTool(),
        GitDiffTool(),
        LinterAutoFixTool(),
        DependencyInstallTool(),
        DataPipelineRunTool(),
    ]
    for tool in tools:
        # Inject the per-session gate context onto the PrivateAttr declared by
        # _GatedExecTool. BaseTool is the declared element type, so the attribute
        # is invisible to mypy here.
        tool._session_ctx = ctx  # type: ignore[attr-defined]
    return tools
