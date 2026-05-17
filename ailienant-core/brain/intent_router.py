"""Phase 4.3 stage-2 — top-level execution mode dispatcher.

Three branches, each delegating to a pre-compiled artifact:

  * SEQUENTIAL  → :func:`brain.fast_path.execute_sequential_bypass`
  * MICRO_SWARM → ``brain.swarms._MICRO_SWARM_APP.ainvoke``
  * FULL_SWARM  → ``brain.swarms.build_full_swarm(checkpoint_manager).ainvoke``

The router never instantiates an LLM. Mode lock-in: ``execution_mode`` is set
once on the inbound state dict and is never mutated by downstream nodes — this
keeps KV-cache invalidations to a single transition per run.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("INTENT_ROUTER")


async def process_user_intent(
    prompt: str,
    workspace_root: str,
    task_id: str = "",
    execution_mode: str = "sequential",
) -> Dict[str, Any]:
    """Dispatch to the SEQUENTIAL / MICRO_SWARM / FULL_SWARM execution tier.

    Returns the graph-shape dict the caller expects to broadcast via
    ``websocket_manager.broadcast_token()`` (keys: ``messages``, optionally
    ``shared_understanding_reached``).
    """
    mode = execution_mode.strip().upper()
    logger.info("process_user_intent: mode=%s task_id=%s", mode, task_id)

    if mode == "SEQUENTIAL":
        from brain.fast_path import execute_sequential_bypass

        return await execute_sequential_bypass(
            prompt=prompt,
            workspace_root=workspace_root,
            task_id=task_id,
        )

    if mode == "MICRO_SWARM":
        from brain.swarms import _MICRO_SWARM_APP

        initial: Dict[str, Any] = {
            "task_id": task_id,
            "user_input": prompt,
            "workspace_root": workspace_root,
            "execution_mode": "MICRO_SWARM",
            "messages": [{"role": "user", "content": prompt}],
            "retry_count": 0,
            "error_streak": 0,
            "consecutive_style_failures": 0,
            "cloud_surgeon_invocations": 0,
            "circuit_breaker_tripped": False,
            "style_bypass_active": False,
            "syntax_gate_status": "pending",
            "style_gate_status": "pending",
        }
        return await _MICRO_SWARM_APP.ainvoke(initial)

    if mode == "FULL_SWARM":
        from brain.checkpoint import checkpoint_manager
        from brain.swarms import build_full_swarm

        app = build_full_swarm(checkpoint_manager)
        initial = {
            "task_id": task_id,
            "user_input": prompt,
            "workspace_root": workspace_root,
            "execution_mode": "FULL_SWARM",
            "messages": [{"role": "user", "content": prompt}],
            "retry_count": 0,
            "error_streak": 0,
        }
        config = {"configurable": {"thread_id": task_id}} if task_id else None
        return await app.ainvoke(initial, config=config)

    raise NotImplementedError(f"Execution mode '{mode}' is not recognised.")
