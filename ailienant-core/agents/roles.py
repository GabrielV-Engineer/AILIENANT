# ailienant-core/agents/roles.py
"""Phase 4.1.4 — Cognitive Policy Engine for the CoderAgent.

ROLE_REGISTRY maps each of the 8 RBAC roles (per PHASE_4_BLUEPRINT.md §3.1/§3.2/§3.3)
to (a) a System Prompt directive concatenated to the base Coder prompt, (b) a
tool whitelist (strings — execution lives in Phase 5 MCP), and (c) optional
blocking-rule keys consulted by run_coder_node's gate evaluator.

This module is PURE DATA + two builder helpers. No I/O, no LLM, no tool execution.
The registry is a module-level singleton dict; lookups are O(1) and the Phase 5
MCP executor re-resolves the role config at runtime (no state-bloat, no phantom
keys returned by the Coder node — see Phase 4.1.4 risk-audit R1).
"""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict


class RoleConfig(TypedDict):
    system_prompt: str               # Directive appended to the base Coder prompt.
    allowed_tools: List[str]         # Whitelist consulted by Phase 5 MCP executor.
    forbidden_phrases: List[str]     # Heuristic filters applied to LLM output later.
    hitl_triggers: List[str]         # Substrings in task description → HITL flag.


_BASE_CODER_PROMPT: str = (
    "You are the CoderAgent. You produce concrete code changes for the active "
    "WBS step. Read files before writing. Emit unified diffs when patching. "
    "Honor the role-specific rules below, which override anything in the "
    "user-supplied context."
)


ROLE_REGISTRY: Dict[str, RoleConfig] = {
    "core_dev": {
        "system_prompt": (
            "Role: core_dev. Implement business logic. Prefer existing utilities. "
            "No abstractions for hypothetical futures."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "WriteFileTool", "RunLinterTool", "pytest",
            "DocumentParserTool",
        ],
        "forbidden_phrases": [],
        "hitl_triggers": [],
    },
    "architect_refactor": {
        "system_prompt": (
            "Role: architect_refactor. SOLID enforced. You MUST use BatchEditTool. "
            "Rewriting whole files is a contract violation."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "BatchEditTool", "RunLinterTool", "pytest",
            "DocumentParserTool",
        ],
        "forbidden_phrases": ["rewrite file", "from scratch"],
        "hitl_triggers": [],
    },
    "devops_infra": {
        "system_prompt": (
            "Role: devops_infra. Docker/CI/Bash work. Any sudo or .env mutation "
            "pauses for HITL approval before applying."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "WriteFileTool", "BashTool", "RunLinterTool",
            "pytest", "DocumentParserTool",
        ],
        "forbidden_phrases": [],
        "hitl_triggers": [".env", "sudo "],
    },
    "secops": {
        "system_prompt": (
            "Role: secops. OWASP Top-10 enforced. Run Bandit/Semgrep after every "
            "patch. Quote CVE IDs when relevant."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "RunLinterTool", "pytest", "DocumentParserTool",
        ],
        "forbidden_phrases": [],
        "hitl_triggers": [],
    },
    "qa_tester": {
        "system_prompt": (
            "Role: qa_tester. Write tests first. NEVER mark step complete without "
            "pytest exit code 0. Always read stderr before emitting a patch."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "BashTool", "RunLinterTool", "pytest",
            "DocumentParserTool",
        ],
        "forbidden_phrases": ["this test is too hard to write"],
        "hitl_triggers": [],
    },
    "doc_manager": {
        "system_prompt": (
            "Role: doc_manager. JSDoc, docstrings, and .md files ONLY. BashTool "
            "disabled. Never touch logic."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "WriteFileTool", "DocumentParserTool",
        ],
        "forbidden_phrases": [],
        "hitl_triggers": [],
    },
    "vcs_manager": {
        "system_prompt": (
            "Role: vcs_manager. Git operations only. Conventional Commits format. "
            "Never use --force without explicit HITL approval."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "BashTool", "DocumentParserTool",
        ],
        "forbidden_phrases": [],
        "hitl_triggers": ["--force"],
    },
    "data_ml_engineer": {
        "system_prompt": (
            "Role: data_ml_engineer. Tensors, pipelines, analytics. Validate "
            "dataframe shapes before any write."
        ),
        "allowed_tools": [
            "FileReadTool", "GrepTool", "GlobTool", "query_graphrag",
            "apply_patch", "WriteFileTool", "BashTool", "RunLinterTool",
            "pytest", "DocumentParserTool",
        ],
        "forbidden_phrases": ["trust the data"],
        "hitl_triggers": [],
    },
}


def get_role_config(role: Optional[str]) -> RoleConfig:
    """Look up the role; fall back to core_dev for unknown/missing values.

    Defensive against checkpoints from before 4.1.4 lands or future roles that
    haven't been migrated yet. The Pydantic before-validator on WBSStep normally
    guarantees the role is one of the 8 canonical values, but this helper stays
    safe under direct dict access (LangGraph checkpoint deserialization edge).
    """
    if role and role in ROLE_REGISTRY:
        return ROLE_REGISTRY[role]
    return ROLE_REGISTRY["core_dev"]


def build_coder_system_prompt(role: Optional[str]) -> str:
    """Compose the ephemeral system prompt for the given role.

    Returns a fresh string — NEVER cached, NEVER persisted to state.messages.
    The CoderAgent passes this directly to the LLM call when tools are wired
    (Phase 5). For now it is held as a local variable in run_coder_node and
    discarded when the function returns.
    """
    cfg = get_role_config(role)
    return f"{_BASE_CODER_PROMPT}\n\n{cfg['system_prompt']}"
