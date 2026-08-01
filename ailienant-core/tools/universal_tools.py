"""Universal tools — available to every cognitive role.

A universal tool is one any agent may hold regardless of its RBAC role. Two live here
conceptually: `tool_search` (the discovery primitive, registered by ``meta_tools`` and
cross-listed here only by widening its ``allowed_roles`` to the full role universe) and
`todo_write` (this module's net-new contribution).

``TodoWriteTool`` (``todo_write``) lets an agent publish a structured task scratchpad — the
same content/status/active_form shape a human operator sees as a checklist. It is the
agent's own planning surface: write the full list, mark one item in-progress, complete it,
and clear the list when finished. The tool is READ_ONLY (a TODO list is cognitive
scaffolding, not a filesystem / execution / ledger side effect), so the session-mode filter
admits it in every mode including PLAN, exactly as `tool_search` is admitted.

The tool normalizes deterministically and returns a JSON string whose ``agent_todos`` key
maps onto the ``AIlienantGraphState.agent_todos`` channel; a future graph node decodes it and
commits the delta through the channel's replace-semantics reducer. ``_arun`` itself never
touches state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, FrozenSet, List, Literal, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.control_tools import ALL_ROLES

logger = logging.getLogger("UNIVERSAL_TOOLS")

# ── Output caps (token hygiene §5.5) ──────────────────────────────────────────
_MAX_TODOS: int = 50  # bound the list so a runaway plan can't flood the context window
# Per-field char ceiling. Structural backstop for the state-channel promotion path
# (core/tool_dispatch.py::promote_tool_state / shared.config.MAX_JSON_PARSE_CHARS):
# without this, an unbounded str field lets 50 items of megabyte-scale text serialize
# into a payload that ceiling has to reject outright. Truncated, not rejected — a
# verbose model must never turn into a hard tool failure.
_MAX_FIELD_CHARS: int = 300

# ── Role assignment ───────────────────────────────────────────────────────────
_UNIVERSAL_ROLES: FrozenSet[str] = ALL_ROLES


# =====================================================================
# TodoWriteTool
# =====================================================================


class TodoItem(BaseModel):
    """One task in an agent's TODO list."""

    content: str = Field(
        min_length=1,
        description="The task, in imperative form (e.g. 'Add the retry guard').",
    )
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="Lifecycle state. At most one item should be 'in_progress' at a time.",
    )
    active_form: str = Field(
        min_length=1,
        description="Present-continuous label shown while active (e.g. 'Adding the retry guard').",
    )


class TodoWriteInput(BaseModel):
    todos: List[TodoItem] = Field(
        description="The full TODO list. Each call replaces the previous list in its entirety; "
        "send an empty list to clear it once every task is complete.",
    )


class TodoWriteTool(BaseTool):
    """Publish the agent's structured TODO list to shared state.

    READ_ONLY and available to every role, so it is admitted under any session mode
    (including PLAN). Normalizes deterministically — caps the list length and enforces the
    single-active invariant — then returns the canonical list as a JSON string keyed for the
    ``agent_todos`` channel.
    """

    name: str = "todo_write"
    description: str = (
        "Write your task TODO list to shared state. Send the FULL list every call (it "
        "replaces the previous one); mark exactly one item 'in_progress' while you work on "
        "it, set items 'completed' as you finish, and send an empty list to clear the panel "
        "once everything is done."
    )
    args_schema: Type[BaseModel] = TodoWriteInput  # pyright: ignore[reportIncompatibleVariableOverride]

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TodoWriteTool is async-only — use _arun().")

    async def _arun(self, todos: List[Any]) -> str:
        # 1. Coerce raw dict items to TodoItem, then cap the list (token hygiene
        # §5.5). core/tool_dispatch.py::ToolDispatcher.dispatch calls _arun
        # directly with **call.args parsed from the model's raw JSON envelope —
        # it does not route through LangChain's args_schema validation, so an
        # item may arrive as a plain dict rather than a TodoItem instance. Without
        # this coercion, any real (non-empty) todo_write call reaching that path
        # would AttributeError on the first `.status` access below; a single
        # malformed item is dropped rather than failing the whole write.
        coerced: List[TodoItem] = []
        for raw_item in todos:
            if isinstance(raw_item, TodoItem):
                coerced.append(raw_item)
                continue
            try:
                coerced.append(TodoItem.model_validate(raw_item))
            except Exception as exc:  # noqa: BLE001 — one bad item must not fail the whole write
                logger.warning("todo_write: dropped a malformed item: %s", exc, exc_info=True)
        normalized: List[TodoItem] = coerced[:_MAX_TODOS]

        # 1b. Truncate (never reject) each free-text field — structural bound so the
        # state-promotion path never has to discard an otherwise-valid write for size.
        for item in normalized:
            if len(item.content) > _MAX_FIELD_CHARS:
                item.content = item.content[:_MAX_FIELD_CHARS]
            if len(item.active_form) > _MAX_FIELD_CHARS:
                item.active_form = item.active_form[:_MAX_FIELD_CHARS]

        # 2. Single-active invariant: keep the first 'in_progress' in list order, demote any
        #    later 'in_progress' to 'pending'. Deterministic by position.
        seen_active = False
        for item in normalized:
            if item.status == "in_progress":
                if seen_active:
                    item.status = "pending"
                else:
                    seen_active = True

        payload = {
            # `agent_todos` names the AIlienantGraphState channel; a future graph node
            # json.loads()-es this and writes the delta through the _merge_todos reducer.
            "agent_todos": [item.model_dump() for item in normalized],
            "count": len(normalized),
        }
        logger.info("todo_write: %d item(s) (active=%s)", len(normalized), seen_active)
        return json.dumps(payload)


# =====================================================================
# Schema registration
# =====================================================================


def _tool_schema(
    name: str, description: str, input_model: Type[BaseModel]
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=_UNIVERSAL_ROLES,
    )


async def register_universal_tools(store: ToolRAGStore) -> int:
    """Register the net-new universal schema(s) in the given store. Returns count.

    Only ``todo_write`` is registered here. ``tool_search`` is registered by
    ``meta_tools.register_meta_tools`` and is merely cross-listed to the same role universe
    (via its ``allowed_roles``), never duplicated.
    """
    schemas: List[ToolSchema] = [
        _tool_schema(
            "todo_write",
            "Publish the agent's structured TODO list (content / status / active_form) to "
            "shared state; replaces the prior list each call, empty clears it.",
            TodoWriteInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
