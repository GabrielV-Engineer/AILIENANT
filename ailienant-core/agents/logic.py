# ailienant-core/agents/logic.py
#
# Phase 2.20 — LogicAgent (tool-enabled execution node stub).
#
# Phase 2.20: thin wrapper over run_coder_node; establishes the agent boundary.
# Phase 4 upgrade path:
#   1. Call tool_registry.bind_tools(llm, AgentRole.CODER) to attach VFS tools.
#   2. Replace delegation with a real LLM call + structured output parsing.
#   3. Write VFSFile objects (blob_hash + document_version_id) into vfs_buffer.

import logging
from agents.coder import run_coder_node

logger = logging.getLogger("LOGIC_AGENT")


async def run_logic_node(state: dict) -> dict:
    """LangGraph node: tool-enabled execution agent.

    Phase 2.20: delegates to run_coder_node (same step-marking logic).
    Phase 4: real LLM call with bind_tools(AgentRole.CODER) + VFS writes.
    """
    logger.info(
        "LogicAgent: step=%s role=%s — delegating to CoderAgent stub (Phase 2.20).",
        state.get("current_step_id"), state.get("target_role"),
    )
    return await run_coder_node(state)
