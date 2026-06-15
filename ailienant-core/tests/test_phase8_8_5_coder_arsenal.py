"""Wave 4 Coder Arsenal gate — sibling-file checkpoint.

DoD (RBAC parity at the select_tools / allowed_roles surface):
  - 11 net coder schemas register; each net-new tool is gated to its exclusive
    owning role(s); validate_ast is open to every apply_patch-holding role.
  - A role cannot invoke a tool outside its set (negative RBAC), and the formalize
    tools' allowed_roles now mirror agents/roles.py per capability.
  - Tier integrity: only security_audit + validate_ast are READ_ONLY (survive PLAN);
    git_diff is EXECUTE (it spawns git); guard_env_file is DANGEROUS.

Plus hardened-wrapper unit coverage (Zero-Trust Bash):
  - _safe_arg blocks flag injection, path traversal, and absolute paths.
  - install_dependency rejects bad names/versions; git_commit composes conditionally
    and rejects invalid parts; generate_docstring never crashes on a SyntaxError;
    guard_env_file matches by basename and is content-hash idempotent.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Dict, List
from unittest.mock import AsyncMock

import pytest

from core.permissions import SessionPermissionMode, ToolPrivilegeTier
from core.sandbox import SandboxResult
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.coder_tools import (
    _APPLY_PATCH_ROLES,
    ASTValidateTool,
    DependencyInstallTool,
    DocstringGeneratorTool,
    GitCommitTool,
    GitDiffTool,
    GuardEnvFileTool,
    RunTestsTool,
    SecurityAuditTool,
    _ArgRejected,
    _safe_arg,
    register_coder_tools,
)
from tools.execution_tools import register_execution_tools
from tools.mutation_tools import register_mutation_tools


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8."""

    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag_885"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


async def _register_all(store: ToolRAGStore) -> None:
    await register_coder_tools(store)
    await register_mutation_tools(store)
    await register_execution_tools(store)


def _by_name(store: ToolRAGStore) -> Dict[str, ToolSchema]:
    return {s.name: s for s in store.all_schemas()}


class _FakeAdapter:
    """Records dispatched commands; returns a deterministic success result."""

    def __init__(self) -> None:
        self.commands: List[str] = []

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: str | None = None,
    ) -> SandboxResult:
        self.commands.append(command)
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


# =====================================================================
# A — Registration + RBAC parity (allowed_roles is the enforcement surface)
# =====================================================================


@pytest.mark.anyio
async def test_register_coder_tools_registers_eleven(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    count = await register_coder_tools(store)
    assert count == 11
    names = set(_by_name(store))
    assert names == {
        "run_tests",
        "git_stage",
        "git_commit",
        "git_diff",
        "generate_docstring",
        "linter_autofix",
        "install_dependency",
        "guard_env_file",
        "run_data_pipeline",
        "security_audit",
        "validate_ast",
    }


@pytest.mark.anyio
async def test_exclusive_owning_roles(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_coder_tools(store)
    roles = {n: s.allowed_roles for n, s in _by_name(store).items()}
    assert roles["run_tests"] == frozenset({"qa_tester"})
    assert roles["git_stage"] == frozenset({"vcs_manager"})
    assert roles["git_commit"] == frozenset({"vcs_manager"})
    assert roles["git_diff"] == frozenset({"vcs_manager"})
    assert roles["generate_docstring"] == frozenset({"doc_manager"})
    assert roles["linter_autofix"] == frozenset({"secops", "qa_tester"})
    assert roles["install_dependency"] == frozenset({"devops_infra"})
    assert roles["guard_env_file"] == frozenset({"devops_infra"})
    assert roles["run_data_pipeline"] == frozenset({"data_ml_engineer"})
    assert roles["security_audit"] == frozenset({"secops"})
    assert roles["validate_ast"] == _APPLY_PATCH_ROLES


@pytest.mark.anyio
async def test_negative_rbac_cross_role(tmp_path: Path) -> None:
    """A role cannot invoke a tool outside its set (the DoD)."""
    store = _isolated_store(tmp_path)
    await register_coder_tools(store)
    roles = {n: s.allowed_roles for n, s in _by_name(store).items()}
    assert "doc_manager" not in roles["run_tests"]
    assert "qa_tester" not in roles["git_commit"]
    assert "data_ml_engineer" not in roles["security_audit"]
    assert "vcs_manager" not in roles["validate_ast"]  # vcs has no apply_patch
    assert "core_dev" not in roles["run_data_pipeline"]


# =====================================================================
# B — Formalize: strict per-role mirror of agents/roles.py
# =====================================================================


@pytest.mark.anyio
async def test_formalize_mutation_role_mirror(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_mutation_tools(store)
    roles = {n: s.allowed_roles for n, s in _by_name(store).items()}
    assert roles["batch_semantic_edit"] == frozenset({"architect_refactor"})
    assert roles["file_write"] == frozenset(
        {"core_dev", "devops_infra", "doc_manager", "data_ml_engineer"}
    )
    # apply_patch holders = everyone except vcs_manager.
    assert "qa_tester" in roles["atomic_code_patch"]
    assert "doc_manager" in roles["atomic_code_patch"]
    assert "vcs_manager" not in roles["atomic_code_patch"]
    # secops is NOT a WriteFileTool holder.
    assert "secops" not in roles["file_write"]


@pytest.mark.anyio
async def test_formalize_sandbox_bash_role_mirror(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_execution_tools(store)
    roles = _by_name(store)["sandbox_bash"].allowed_roles
    assert roles == frozenset(
        {"devops_infra", "qa_tester", "vcs_manager", "data_ml_engineer"}
    )
    assert "vcs_manager" in roles  # gained (git ops)
    assert "core_dev" not in roles  # lost (no BashTool in roles.py)
    assert "secops" not in roles


# =====================================================================
# C — Tier integrity + PLAN survival
# =====================================================================


@pytest.mark.anyio
async def test_tier_assignment(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_coder_tools(store)
    tiers = {n: s.privilege_tier for n, s in _by_name(store).items()}
    read_only = {n for n, t in tiers.items() if t is ToolPrivilegeTier.READ_ONLY}
    assert read_only == {"security_audit", "validate_ast"}
    assert tiers["git_diff"] is ToolPrivilegeTier.EXECUTE  # spawns git
    assert tiers["guard_env_file"] is ToolPrivilegeTier.DANGEROUS
    assert tiers["generate_docstring"] is ToolPrivilegeTier.WRITE
    assert tiers["run_tests"] is ToolPrivilegeTier.EXECUTE


@pytest.mark.anyio
async def test_plan_mode_admits_only_read_only(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await _register_all(store)
    selected = await store.select_tools(
        intent="review code safety without running anything",
        active_role="secops",
        session_mode=SessionPermissionMode.PLAN,
    )
    assert selected, "secops should retain READ_ONLY tools in PLAN"
    for schema in selected:
        assert schema.privilege_tier is ToolPrivilegeTier.READ_ONLY


@pytest.mark.anyio
async def test_vcs_manager_surfaces_git_tools(tmp_path: Path) -> None:
    """vcs_manager's eligible set is small (<= k), so all git tools surface."""
    store = _isolated_store(tmp_path)
    await _register_all(store)
    selected = await store.select_tools(
        intent="stage, diff and commit my changes",
        active_role="vcs_manager",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    names = {s.name for s in selected}
    assert {"git_stage", "git_commit", "git_diff"} <= names


# =====================================================================
# D — Zero-Trust argument guard
# =====================================================================


def test_safe_arg_accepts_relative_paths() -> None:
    assert _safe_arg("src/main.py") == "src/main.py"
    assert _safe_arg("tests/dir with space.py") == "'tests/dir with space.py'"


@pytest.mark.parametrize(
    "bad",
    ["--config=evil.toml", "-rf", "../../etc/shadow", "src/../../../etc", "/etc/passwd", "\\\\srv", "C:\\Windows"],
)
def test_safe_arg_rejects_hostile(bad: str) -> None:
    with pytest.raises(_ArgRejected):
        _safe_arg(bad)


# =====================================================================
# E — Hardened wrappers (direct _arun)
# =====================================================================


@pytest.mark.anyio
async def test_run_tests_dispatches_with_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    out = await RunTestsTool()._arun(target="tests/unit")
    assert "exit=0" in out
    assert fake.commands == ["pytest -q -- tests/unit"]


@pytest.mark.anyio
async def test_run_tests_rejects_flag_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    out = await RunTestsTool()._arun(target="--collect-only")
    assert out.startswith("[run_tests] REJECTED")
    assert fake.commands == []  # never armed


@pytest.mark.anyio
async def test_install_dependency_supply_chain_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    tool = DependencyInstallTool()
    for bad in ("-e", "evil/pkg", "pkg@http://evil", "--index-url"):
        out = await tool._arun(name=bad)
        assert out.startswith("[install_dependency] REJECTED"), bad
    assert fake.commands == []
    out = await tool._arun(name="requests", version="2.31.0")
    assert fake.commands == ["python -m pip install requests==2.31.0"]
    assert "exit=0" in out


@pytest.mark.anyio
async def test_git_commit_conditional_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    tool = GitCommitTool()
    await tool._arun(commit_type="feat", subject="add parity", scope="coder")
    await tool._arun(commit_type="fix", subject="no scope here")
    assert fake.commands == [
        "git commit -m 'feat(coder): add parity'",
        "git commit -m 'fix: no scope here'",
    ]
    rejected = await tool._arun(commit_type="wip", subject="bad type")
    assert rejected.startswith("[git_commit] REJECTED")
    assert len(fake.commands) == 2  # the rejected one never armed


@pytest.mark.anyio
async def test_git_diff_rejects_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    out = await GitDiffTool()._arun(paths=["../secrets"])
    assert out.startswith("[git_diff] REJECTED")
    assert fake.commands == []


@pytest.mark.anyio
async def test_generate_docstring_happy_path() -> None:
    vfs: Dict[str, str] = {"m.py": "def foo():\n    return 1\n"}
    tool = DocstringGeneratorTool(
        vfs_read=vfs.get,
        vfs_write=lambda p, c: vfs.__setitem__(p, c),
    )
    out = await tool._arun(file_path="m.py", symbol_name="foo")
    assert out.startswith("[generate_docstring] OK")
    assert '"""TODO: document foo."""' in vfs["m.py"]


@pytest.mark.anyio
async def test_generate_docstring_survives_syntax_error() -> None:
    vfs: Dict[str, str] = {"broken.py": "def foo(:\n    pass\n"}
    tool = DocstringGeneratorTool(
        vfs_read=vfs.get,
        vfs_write=lambda p, c: vfs.__setitem__(p, c),
    )
    out = await tool._arun(file_path="broken.py", symbol_name="foo")
    assert json.loads(out) == {"error": "Fix syntax errors before generating docstrings."}
    assert vfs["broken.py"] == "def foo(:\n    pass\n"  # untouched


@pytest.mark.anyio
async def test_generate_docstring_single_line_skips() -> None:
    vfs: Dict[str, str] = {"s.py": "def f(): return 1\n"}
    tool = DocstringGeneratorTool(
        vfs_read=vfs.get,
        vfs_write=lambda p, c: vfs.__setitem__(p, c),
    )
    out = await tool._arun(file_path="s.py", symbol_name="f")
    assert "SKIP" in out


@pytest.mark.anyio
async def test_guard_env_file_basename_and_idempotency() -> None:
    tool = GuardEnvFileTool()
    g1 = await tool._arun(file_path=".env", proposed_content="SECRET=1")
    g2 = await tool._arun(file_path=".env", proposed_content="SECRET=1")
    g3 = await tool._arun(file_path=".env", proposed_content="SECRET=2")
    assert g1.startswith("[guard_env_file] HITL_GATE:")
    assert g1 == g2  # content-hash idempotent
    assert g1 != g3  # different content -> different gate
    nested = await tool._arun(file_path="config/.env.local", proposed_content="X=1")
    assert nested.startswith("[guard_env_file] HITL_GATE:")
    # .environment is NOT an env file (basename match, not substring).
    decoy = await tool._arun(file_path="src/.environment", proposed_content="x")
    assert decoy.startswith("[guard_env_file] PASS")


@pytest.mark.anyio
async def test_security_audit_flags_owasp() -> None:
    tool = SecurityAuditTool()
    flagged = json.loads(await tool._arun(diff='password = "hunter2secret"\nexec(user_in)\n'))
    assert flagged["clean"] is False
    issues = {f["issue"] for f in flagged["findings"]}
    assert "hardcoded_secret" in issues
    assert "dangerous_eval_exec" in issues
    clean = json.loads(await tool._arun(diff="x = 1 + 2\n"))
    assert clean["clean"] is True


@pytest.mark.anyio
async def test_validate_ast_wrapper() -> None:
    tool = ASTValidateTool()
    ok = json.loads(await tool._arun(file_path="a.py", content="x = 1\n"))
    assert ok["is_valid"] is True
    bad = json.loads(await tool._arun(file_path="a.py", content="def (:\n"))
    assert bad["is_valid"] is False
    assert bad["errors"]


# =====================================================================
# F — DEBT-046: session-injecting factory + per-session HITL command gate
# =====================================================================


def _gated_run_tests(state: Dict[str, object]) -> RunTestsTool:
    from tools.coder_tools import make_coder_execute_tools

    tools = {t.name: t for t in make_coder_execute_tools(state)}
    tool = tools["run_tests"]
    assert isinstance(tool, RunTestsTool)
    return tool


def _wire_gate(
    monkeypatch: pytest.MonkeyPatch, fake: "_FakeAdapter", approval: object
) -> AsyncMock:
    from tools import mcp_adapter

    monkeypatch.setattr("tools.coder_tools.get_active_adapter", lambda: fake)
    approval_mock = AsyncMock(return_value=approval)
    monkeypatch.setattr(
        "api.websocket_manager.vfs_manager.request_human_approval", approval_mock
    )
    # Start from a clean per-session trust set so a prior test cannot pre-trust us.
    mcp_adapter.clear_session_trust("sg")
    return approval_mock


@pytest.mark.anyio
async def test_make_coder_execute_tools_excludes_guard_env_file() -> None:
    from tools.coder_tools import make_coder_execute_tools

    names = {t.name for t in make_coder_execute_tools({"session_id": "sg"})}
    assert names == {
        "run_tests", "git_stage", "git_commit", "git_diff",
        "linter_autofix", "install_dependency", "run_data_pipeline",
    }
    assert "guard_env_file" not in names  # keeps its own content-hash HITL gate


@pytest.mark.anyio
async def test_gated_default_mode_prompts_then_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    approval_mock = _wire_gate(monkeypatch, fake, {"approved": True})
    tool = _gated_run_tests({"session_id": "sg", "session_permission_mode": "DEFAULT"})

    out = await tool._arun(target="tests/unit")
    assert "exit=0" in out
    assert fake.commands == ["pytest -q -- tests/unit"]
    approval_mock.assert_awaited_once()
    assert approval_mock.await_args is not None
    assert approval_mock.await_args.kwargs["request_kind"] == "COMMAND_EXEC"


@pytest.mark.anyio
async def test_gated_reject_suppresses_command(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    _wire_gate(monkeypatch, fake, {"approved": False})
    tool = _gated_run_tests({"session_id": "sg", "session_permission_mode": "DEFAULT"})

    out = await tool._arun(target="tests/unit")
    assert "BLOCKED" in out
    assert fake.commands == []  # never armed — the gate denied it


@pytest.mark.anyio
async def test_gated_trust_once_skips_second_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    approval_mock = _wire_gate(monkeypatch, fake, {"approved": True})
    tool = _gated_run_tests({"session_id": "sg", "session_permission_mode": "DEFAULT"})

    await tool._arun(target="a")
    await tool._arun(target="b")
    approval_mock.assert_awaited_once()  # trusted after the first approval
    assert fake.commands == ["pytest -q -- a", "pytest -q -- b"]


@pytest.mark.anyio
async def test_gated_plan_mode_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    approval_mock = _wire_gate(monkeypatch, fake, {"approved": True})
    tool = _gated_run_tests({"session_id": "sg", "session_permission_mode": "PLAN"})

    out = await tool._arun(target="tests/unit")
    assert "DENIED" in out
    assert fake.commands == []
    approval_mock.assert_not_awaited()  # plan mode short-circuits before any prompt


@pytest.mark.anyio
async def test_gated_auto_mode_runs_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAdapter()
    approval_mock = _wire_gate(monkeypatch, fake, {"approved": True})
    tool = _gated_run_tests({"session_id": "sg", "session_permission_mode": "AUTO"})

    out = await tool._arun(target="tests/unit")
    assert "exit=0" in out
    assert fake.commands == ["pytest -q -- tests/unit"]
    approval_mock.assert_not_awaited()  # AUTO admits EXECUTE without HITL
