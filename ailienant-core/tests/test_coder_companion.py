# ailienant-core/tests/test_coder_companion.py
#
# Phase 11.5.B — Coder Companion Tests
# Coverage: background-task lifecycle, LLM call resilience, parsing, UI safety.

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from typing import Dict, Any

pytestmark = pytest.mark.anyio

from brain.coder_companion import (
    schedule_coder_companion,
    _companion_background_tasks,
    _resolve_verbosity,
    _call_analyst_llm,
    _parse_companion_json,
    _run_coder_companion,
    CompanionAnalysis,
    CompanionAnalysisRequest,
    _companion_semaphore,
)


# ─── FIXTURES ────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_state() -> Dict[str, Any]:
    """Minimal AIlienantGraphState for testing."""
    return {
        "task_id": "test-task-123",
        "current_step_id": 1,
        "project_id": "proj-456",
        "workspace_root": "/workspace",
        "mission_spec": None,
        "pending_patches": {
            "src/main.py": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new",
        },
        "pending_contents": {"src/main.py": "new content"},
        "errors": [],
        "security_flags": [],
        "current_cost_usd": 0.0,
        "max_budget_usd": 100.0,
        "retry_count": 0,
    }


# ─── UNIT TESTS: _resolve_verbosity ──────────────────────────────────────────

class TestResolveVerbosity:
    """Verbosity heuristic truth table."""

    def test_deep_with_errors(self, mock_state):
        """Errors present → deep verbosity."""
        mock_state["errors"] = ["error1"]
        assert _resolve_verbosity(mock_state) == "deep"

    def test_deep_with_security_flags(self, mock_state):
        """Security flags present → deep verbosity."""
        mock_state["security_flags"] = ["FLAG_SECURITY"]
        assert _resolve_verbosity(mock_state) == "deep"

    def test_deep_with_many_patches(self, mock_state):
        """More than 3 files → deep verbosity."""
        mock_state["pending_patches"] = {
            f"file{i}.py": f"diff{i}" for i in range(5)
        }
        assert _resolve_verbosity(mock_state) == "deep"

    def test_minimal_single_file_no_errors(self, mock_state):
        """Single file + no errors → minimal verbosity."""
        mock_state["pending_patches"] = {"src/main.py": "diff"}
        mock_state["errors"] = []
        assert _resolve_verbosity(mock_state) == "minimal"

    def test_normal_two_files(self, mock_state):
        """Two files + no flags/errors → normal verbosity."""
        mock_state["pending_patches"] = {"file1.py": "diff1", "file2.py": "diff2"}
        mock_state["errors"] = []
        mock_state["security_flags"] = []
        assert _resolve_verbosity(mock_state) == "normal"


# ─── UNIT TESTS: _parse_companion_json ───────────────────────────────────────

class TestParseCompanionJson:
    """JSON parsing with semantic-empty guard."""

    def test_valid_json_parses(self):
        """Valid JSON deserializes to CompanionAnalysis."""
        raw = '{"objective": "Fix bug X", "decisions": []}'
        result = _parse_companion_json(raw)
        assert result.objective == "Fix bug X"
        assert not result.degraded

    def test_malformed_json_degrades(self):
        """Malformed JSON → degraded=True."""
        raw = "this is not json at all"
        result = _parse_companion_json(raw)
        assert result.degraded
        assert "unavailable" in result.objective.lower()

    def test_empty_objective_degrades(self):
        """Valid JSON but empty objective → degraded=True."""
        raw = '{"objective": "", "decisions": []}'
        result = _parse_companion_json(raw)
        assert result.degraded

    def test_whitespace_only_objective_degrades(self):
        """Valid JSON but whitespace-only objective → degraded=True."""
        raw = '{"objective": "   ", "decisions": []}'
        result = _parse_companion_json(raw)
        assert result.degraded

    def test_extra_keys_tolerated(self):
        """Extra unknown fields tolerated per extra='ignore'."""
        raw = '{"objective": "Fix bug", "unknown_field": "value", "decisions": []}'
        result = _parse_companion_json(raw)
        assert result.objective == "Fix bug"
        assert not result.degraded


# ─── UNIT TESTS: _call_analyst_llm ───────────────────────────────────────────

async def test_call_analyst_llm_success():
    """Successful LLM call returns parsed CompanionAnalysis."""
    request = CompanionAnalysisRequest(
        session_id="sess1",
        task_id="task1",
        attempt_ordinal=0,
        task_description="Fix bug",
        pending_patches={"file.py": "diff"},
        pending_contents={"file.py": "content"},
        file_context={},
        relevant_errors=[],
        security_flags=[],
        verbosity="normal",
    )

    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content='{"objective": "Done", "decisions": []}'))]

    with patch("brain.coder_companion.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = mock_response
        with patch("core.response_cache.response_cache.probe", return_value=None):
            with patch("core.response_cache.response_cache.store"):
                result = await _call_analyst_llm(request)

    assert result.objective == "Done"
    assert not result.degraded


async def test_call_analyst_llm_timeout_degrades():
    """LLM call timeout → degraded=True (via asyncio.wait_for)."""
    request = CompanionAnalysisRequest(
        session_id="sess1",
        task_id="task1",
        attempt_ordinal=0,
        task_description="Fix bug",
        pending_patches={"file.py": "diff"},
        pending_contents={"file.py": "content"},
        file_context={},
        relevant_errors=[],
        security_flags=[],
        verbosity="normal",
    )

    async def hang():
        await asyncio.sleep(100)

    # Force the cloud deadline (12s) deterministically — without this, whether the
    # judge alias resolves to a local target (45s) depends on ambient global BYOM
    # preset state, which would make this test's real wall-clock duration flaky.
    with patch("core.config.model_resolver.get_chat_target", return_value=None):
        with patch("brain.coder_companion.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = hang
            with patch("core.response_cache.response_cache.probe", return_value=None):
                result = await _call_analyst_llm(request)

    assert result.degraded
    assert "unavailable" in result.objective.lower()


async def test_call_analyst_llm_exception_degrades():
    """LLM provider exception → degraded=True (caught by outer try/except)."""
    request = CompanionAnalysisRequest(
        session_id="sess1",
        task_id="task1",
        attempt_ordinal=0,
        task_description="Fix bug",
        pending_patches={"file.py": "diff"},
        pending_contents={"file.py": "content"},
        file_context={},
        relevant_errors=[],
        security_flags=[],
        verbosity="normal",
    )

    with patch("brain.coder_companion.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = RuntimeError("Provider down")
        with patch("core.response_cache.response_cache.probe", return_value=None):
            result = await _call_analyst_llm(request)

    assert result.degraded


async def test_call_analyst_llm_cache_reuse():
    """Identical request hits cache probe; LLM not called twice."""
    request = CompanionAnalysisRequest(
        session_id="sess1",
        task_id="task1",
        attempt_ordinal=0,
        task_description="Fix bug",
        pending_patches={"file.py": "diff"},
        pending_contents={"file.py": "content"},
        file_context={},
        relevant_errors=[],
        security_flags=[],
        verbosity="normal",
    )

    cached_response = '{"objective": "Cached", "decisions": []}'

    with patch("brain.coder_companion.LLMGateway.ainvoke", new_callable=AsyncMock) as mock_llm:
        with patch("core.response_cache.response_cache.probe", return_value=cached_response):
            result = await _call_analyst_llm(request)

    assert result.objective == "Cached"
    mock_llm.assert_not_called()


# ─── INTEGRATION TESTS: _run_coder_companion ──────────────────────────────────

async def test_run_coder_companion_outer_guard_catches_exceptions(mock_state):
    """Outer try/except catches pipeline faults, never raises, and still emits a
    terminal (degraded) broadcast — the frontend card must never be left waiting on
    a producer that silently died."""
    with patch("brain.coder_companion._build_companion_request") as mock_build:
        mock_build.side_effect = RuntimeError("Request build failed")
        with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
            with patch("brain.coder_companion.logger") as mock_logger:
                # Should not raise, should log the exception
                await _run_coder_companion(mock_state, attempt_ordinal=0)
                mock_logger.warning.assert_called_once()
                assert "pipeline failed" in mock_logger.warning.call_args[0][0]

    mock_broadcast.assert_called_once()
    assert mock_broadcast.call_args[1]["analysis"].degraded is True


async def test_run_coder_companion_budget_skip(mock_state):
    """Budget ceiling reached → skip the LLM call, but still broadcast a degraded
    terminal event (not silence) so the frontend never hangs on a card that will
    never resolve on its own."""
    mock_state["current_cost_usd"] = 100.0
    mock_state["max_budget_usd"] = 50.0

    with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
        await _run_coder_companion(mock_state, attempt_ordinal=0)

    mock_broadcast.assert_called_once()
    assert mock_broadcast.call_args[1]["analysis"].degraded is True


async def test_run_coder_companion_broadcasts_on_success(mock_state):
    """Successful run broadcasts explanation event."""
    mock_analysis = CompanionAnalysis(objective="Fixed bug X", degraded=False)

    with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = mock_analysis
        with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
            with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock) as mock_gpu:
                mock_gpu.return_value = True
                await _run_coder_companion(mock_state, attempt_ordinal=0)

    mock_broadcast.assert_called_once()
    call_kwargs = mock_broadcast.call_args[1]
    assert call_kwargs["correlation_id"] == "test-task-123:0"
    assert call_kwargs["analysis"] == mock_analysis


# ─── INTEGRATION TESTS: schedule_coder_companion ──────────────────────────────

async def test_schedule_coder_companion_gc_safety(mock_state):
    """Task added to strong-ref set immediately; removed after completion."""
    # Clear the set to start clean.
    _companion_background_tasks.clear()

    async def quick_task(state, ordinal):
        pass

    with patch("brain.coder_companion._run_coder_companion", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = quick_task
        schedule_coder_companion(mock_state, attempt_ordinal=0)

        # Task should be in the set right after scheduling.
        assert len(_companion_background_tasks) == 1

        # Wait for the task to complete.
        await asyncio.gather(*_companion_background_tasks)

        # Task should be discarded from the set after completion.
        assert len(_companion_background_tasks) == 0


async def test_companion_semaphore_bounds_concurrency():
    """Semaphore limits concurrent Companion calls (MAX_CONCURRENT_COMPANIONS=3)."""
    concurrent_count = 0
    max_concurrent = 0

    async def slow_llm_call(request):
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        await asyncio.sleep(0.05)
        concurrent_count -= 1
        return CompanionAnalysis(objective="Done", degraded=False)

    _companion_background_tasks.clear()

    with patch("brain.coder_companion._call_analyst_llm", side_effect=slow_llm_call):
        with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock):
            with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock) as mock_gpu:
                mock_gpu.return_value = True

                # Schedule 6 companion tasks; semaphore should limit to 3 concurrent.
                for i in range(6):
                    mock_state = {
                        "task_id": f"task{i}",
                        "current_step_id": 1,
                        "project_id": "proj",
                        "workspace_root": "/workspace",
                        "mission_spec": None,
                        "current_cost_usd": 0.0,
                        "max_budget_usd": 100.0,
                        "pending_patches": {},
                        "pending_contents": {},
                        "errors": [],
                        "security_flags": [],
                    }
                    schedule_coder_companion(mock_state, attempt_ordinal=0)

                # Wait for all to complete.
                await asyncio.gather(*_companion_background_tasks)

    assert max_concurrent <= 3, f"Expected ≤3 concurrent, got {max_concurrent}"


# ─── TOKEN-HYGIENE TESTS ───────────────────────────────────────────────────────

def test_token_hygiene_truncation_many_files(mock_state):
    """Request with >8 files truncated to MAX_FILES_IN_PAYLOAD."""
    from brain.coder_companion import _build_companion_request, _MAX_FILES_IN_PAYLOAD

    mock_state["pending_patches"] = {
        f"file{i}.py": f"diff{i}" for i in range(15)
    }

    request = _build_companion_request(mock_state, attempt_ordinal=0)
    assert len(request.pending_patches) <= _MAX_FILES_IN_PAYLOAD


def test_token_hygiene_truncation_large_diff(mock_state):
    """Single diff >MAX_DIFF_CHARS_PER_FILE truncated."""
    from brain.coder_companion import _build_companion_user_payload, _MAX_DIFF_CHARS_PER_FILE

    mock_state["pending_patches"] = {
        "large_file.py": "x" * (2 * _MAX_DIFF_CHARS_PER_FILE)
    }

    request = CompanionAnalysisRequest(
        session_id="s1",
        task_id="t1",
        attempt_ordinal=0,
        task_description="test",
        pending_patches=mock_state["pending_patches"],
        pending_contents={},
        file_context={},
        relevant_errors=[],
        security_flags=[],
        verbosity="normal",
    )

    payload = _build_companion_user_payload(request)
    # Payload should not exceed the raw diff by much (only headers + markers).
    assert len(payload) < 3 * _MAX_DIFF_CHARS_PER_FILE


# ─── WS CONTRACT ROUND-TRIP TEST ──────────────────────────────────────────────

async def test_broadcast_coder_companion_contract(mock_state):
    """Payload serializes and validates against CoderCompanionPayload."""
    from api.ws_contracts import ServerCoderCompanionEvent, CoderCompanionPayload

    mock_analysis = CompanionAnalysis(
        objective="Fixed bug X",
        decisions=[],
        security_notes=["Security issue patched"],
        degraded=False,
    )

    with patch("api.websocket_manager.vfs_manager.send_personal_message", new_callable=AsyncMock) as mock_send:
        # Simulate broadcast_coder_companion building and sending the event.
        from api.websocket_manager import ConnectionManager
        from unittest.mock import MagicMock

        # Create a mock ConnectionManager instance and call the broadcast method.
        cm = ConnectionManager()
        cm.send_personal_message = AsyncMock()

        await cm.broadcast_coder_companion(
            session_id="sess1",
            task_id="task1",
            correlation_id="task1:0",
            analysis=mock_analysis,
        )

        # Assert send_personal_message was called with a ServerCoderCompanionEvent.
        cm.send_personal_message.assert_called_once()
        call_args = cm.send_personal_message.call_args
        assert call_args[0][0] == "sess1"
        event = call_args[0][1]
        assert isinstance(event, ServerCoderCompanionEvent)
        assert event.data.objective == "Fixed bug X"
        assert "Security issue patched" in event.data.security_notes
