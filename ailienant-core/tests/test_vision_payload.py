# tests/test_vision_payload.py
"""DEBT-168 — Vision Bypass routing + multimodal payload.

Two independent seams, tested separately:
  1. `tools.llm_gateway._attach_images_to_messages` — the pure content-block
     builder gated on `litellm.supports_vision(model)`, with count/size ceilings.
  2. The researcher's Vision Bypass override — `state["has_images"]` forces
     CLOUD after the cascade settles, regardless of the cascade's own math.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.context_auditor import RiskLevel

pytestmark = pytest.mark.anyio


# ── 1. _attach_images_to_messages — pure content-block builder ───────────────


def _sample_messages() -> List[Dict[str, Any]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "describe this image"},
    ]


def test_no_images_passes_through_unchanged() -> None:
    from tools.llm_gateway import _attach_images_to_messages

    messages = _sample_messages()
    result = _attach_images_to_messages(messages, None, "gpt-4o", "trace-1")
    assert result is messages


def test_vision_model_gets_content_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import llm_gateway

    monkeypatch.setattr(llm_gateway.litellm, "supports_vision", lambda model, **_kw: True)

    messages = _sample_messages()
    images = [{"data": "AAAA", "mime": "image/png"}]
    result = llm_gateway._attach_images_to_messages(messages, images, "gpt-4o", "trace-2")

    assert result is not messages  # original untouched
    assert messages[1]["content"] == "describe this image"  # source unmutated
    user_content = result[1]["content"]
    assert user_content[0] == {"type": "text", "text": "describe this image"}
    assert user_content[1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_non_vision_model_leaves_messages_unchanged_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from tools import llm_gateway

    monkeypatch.setattr(llm_gateway.litellm, "supports_vision", lambda model, **_kw: False)

    messages = _sample_messages()
    images = [{"data": "AAAA", "mime": "image/png"}]
    with caplog.at_level("WARNING", logger="tools.llm_gateway"):
        result = llm_gateway._attach_images_to_messages(
            messages, images, "some/local-model", "trace-3"
        )

    assert result == messages
    assert any("not sent" in rec.message for rec in caplog.records)


def test_over_count_ceiling_refuses_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from tools import llm_gateway

    monkeypatch.setattr(llm_gateway.litellm, "supports_vision", lambda model, **_kw: True)
    monkeypatch.setattr(llm_gateway, "VISION_MAX_IMAGES_PER_CALL", 1)

    messages = _sample_messages()
    images = [{"data": "AAAA", "mime": "image/png"}, {"data": "BBBB", "mime": "image/png"}]
    with caplog.at_level("WARNING", logger="tools.llm_gateway"):
        result = llm_gateway._attach_images_to_messages(messages, images, "gpt-4o", "trace-4")

    assert result == messages
    assert any("refused" in rec.message for rec in caplog.records)


def test_over_size_ceiling_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import llm_gateway

    monkeypatch.setattr(llm_gateway.litellm, "supports_vision", lambda model, **_kw: True)
    monkeypatch.setattr(llm_gateway, "VISION_MAX_TOTAL_BASE64_CHARS", 2)

    messages = _sample_messages()
    images = [{"data": "AAAAAAAAAA", "mime": "image/png"}]
    result = llm_gateway._attach_images_to_messages(messages, images, "gpt-4o", "trace-5")
    assert result == messages


async def test_ainvoke_images_none_is_a_pure_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that never passes `images` (every pre-existing call site) must
    see byte-identical behavior — the new kwarg is additive."""
    from tools.llm_gateway import LLMGateway

    mock_response = MagicMock()
    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)) as mock_acompletion:
        result = await LLMGateway.ainvoke(messages=_sample_messages(), model="ailienant/medium")

    assert result is mock_response
    sent_messages = mock_acompletion.call_args.kwargs["messages"]
    assert sent_messages == _sample_messages()


# ── 2. Researcher Vision Bypass override ──────────────────────────────────────


def _base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "vision-test",
        "user_input": "What is in this screenshot?",
        "workspace_root": "/ws",
        "project_id": "abc123",
        "explicit_mentions": [],
        "errors": [],
        "has_images": True,
    }
    state.update(overrides)
    return state


async def _noop_reasoner(_messages: Sequence[Dict[str, Any]]) -> str:
    return "{}"


def _skeleton_response() -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="## Skeleton"))]
    return resp


async def test_has_images_forces_cloud_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agents.researcher.check_cloud_availability", lambda: True)

    mock_search = AsyncMock(return_value=(0.7, ["core/state.py"], [""]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            context_block="STUB", parsed_files=[], target_files=[], coverage_ratio=1.0,
        )
    )
    state = _base_state()

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_skeleton_response()),
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    assert result["context_metrics"].routing_decision == "CLOUD"
    assert result["provider"] == "CLOUD"
    assert result["routing_warning"] is None


async def test_has_images_without_cloud_warns_and_stays_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agents.researcher.check_cloud_availability", lambda: False)

    mock_search = AsyncMock(return_value=(0.7, ["core/state.py"], [""]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            context_block="STUB", parsed_files=[], target_files=[], coverage_ratio=1.0,
        )
    )
    state = _base_state()

    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_skeleton_response()),
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        result = await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    assert result["context_metrics"].routing_decision == "CLOUD"
    assert result["provider"] == "LOCAL"
    assert result["routing_warning"] is not None
    assert "cannot be processed" in result["routing_warning"]


async def test_researcher_forwards_attachment_data_to_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The researcher's own ainvoke call must receive `images=` built from
    state["attachments"] — the wiring half of DEBT-168, independent of routing."""
    from brain.state import ManualAttachment

    monkeypatch.setattr("agents.researcher.check_cloud_availability", lambda: True)

    mock_search = AsyncMock(return_value=(0.7, ["core/state.py"], [""]))
    mock_deep_parse = AsyncMock(
        return_value=MagicMock(
            context_block="STUB", parsed_files=[], target_files=[], coverage_ratio=1.0,
        )
    )
    state = _base_state(
        attachments=[
            ManualAttachment(type="image", data="AAAA", mime="image/png"),
            ManualAttachment(type="document", content="irrelevant text doc"),
        ],
    )

    mock_ainvoke = AsyncMock(return_value=_skeleton_response())
    with patch("agents.researcher.DEBUG_MODE", False), patch(
        "agents.researcher.is_fast_track_eligible", return_value=False
    ), patch(
        "agents.researcher.audit_task_complexity", new=AsyncMock(return_value=RiskLevel.NONE)
    ), patch(
        "tools.researcher_tools.build_researcher_tools", return_value={}
    ), patch(
        "core.state_manager.load_state_from_markdown", return_value=None
    ), patch(
        "core.state_manager.dump_state_to_markdown", return_value=None
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager"
    ) as mock_sem_cls, patch(
        "core.memory.graphrag_extractor.GraphRAGDynamicExtractor"
    ) as mock_extractor_cls, patch(
        "tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke,
    ):
        mock_sem_cls.return_value.search_with_paths = mock_search
        mock_extractor_cls.return_value.deep_parse = mock_deep_parse

        from agents.researcher import run_researcher_node

        await run_researcher_node(
            state, {"configurable": {"researcher_tool_reasoner": _noop_reasoner}}
        )

    skeleton_call = mock_ainvoke.call_args_list[-1]
    forwarded_images = skeleton_call.kwargs["images"]
    assert forwarded_images == [{"data": "AAAA", "mime": "image/png"}]
