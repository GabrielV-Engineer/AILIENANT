# ailienant-core/tests/test_coder_companion.py
#
# Coder Companion tests.
# Coverage: background-task lifecycle, LLM call resilience, parsing, UI safety.

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch
from typing import Dict, Any

from brain.coder_companion import (
    schedule_coder_companion,
    schedule_agent_companion,
    _companion_background_tasks,
    _companion_emission_counts,
    _resolve_verbosity,
    _resolve_judge_tier,
    _resolve_companion_llm_timeout,
    _ideation_companion_would_contend_local_compute,
    _call_analyst_llm,
    _parse_companion_json,
    _run_coder_companion,
    _run_agent_companion,
    build_ideation_companion_request,
    build_planning_companion_request,
    build_healing_companion_request,
    CompanionAnalysis,
    CompanionAnalysisRequest,
)
from tools.llm_gateway import resolve_local_timeout

pytestmark = pytest.mark.anyio


# ─── FIXTURES ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_emission_counts():
    """`_companion_emission_counts` is a module-level dict shared across the
    whole test session (mirrors the `_MAX_COMPANION_EMISSIONS_PER_TASK`
    backstop's own module-lifetime scope) — reset it per test so one test's
    emissions never count against another's cap, keeping every test hermetic
    regardless of run order."""
    _companion_emission_counts.clear()
    yield
    _companion_emission_counts.clear()


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
    from api.ws_contracts import ServerCoderCompanionEvent

    mock_analysis = CompanionAnalysis(
        objective="Fixed bug X",
        decisions=[],
        security_notes=["Security issue patched"],
        degraded=False,
    )

    with patch("api.websocket_manager.vfs_manager.send_personal_message", new_callable=AsyncMock):
        # Simulate broadcast_coder_companion building and sending the event.
        from api.websocket_manager import ConnectionManager

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


async def test_broadcast_coder_companion_defaults_scope_and_emission_id():
    """A caller that doesn't pass scope/emission_id (the grandfathered shape)
    still gets a coherent, non-null emission_id (falls back to correlation_id)
    and the 'coding' scope default — additive fields, zero behavior change for
    an old call site."""
    from api.websocket_manager import ConnectionManager

    cm = ConnectionManager()
    cm.send_personal_message = AsyncMock()
    await cm.broadcast_coder_companion(
        session_id="sess1", task_id="task1", correlation_id="task1:0",
        analysis=CompanionAnalysis(objective="Done", degraded=False),
    )
    event = cm.send_personal_message.call_args[0][1]
    assert event.data.scope == "coding"
    assert event.data.emission_id == "task1:0"


# ─── 13.0.7 — GENERALIZED SCOPE BUILDERS ──────────────────────────────────────

def test_build_ideation_companion_request_uses_only_this_rounds_batch():
    """The ideation builder must consume ONLY the round-local batch/answers it's
    handed — never anything accumulated across rounds — so its prompt can never
    grow the way re-sending an append-only state list would."""
    batch = [
        {"id": "q1", "header": "Auth", "question": "Which auth scheme?"},
        {"id": "q2", "header": "Storage", "question": "Which datastore?"},
    ]
    answers = {"q1": "OAuth2", "q2": "Postgres"}
    request = build_ideation_companion_request(
        session_id="sess1", task_id="task1", attempt_ordinal=0,
        task_description="Design the auth flow", question_batch=batch, resolved_answers=answers,
    )
    assert request.scope == "ideation"
    assert "OAuth2" in request.scope_summary
    assert "Postgres" in request.scope_summary
    assert len(request.scope_summary) <= 4000


def test_build_planning_companion_request_summarizes_the_committed_plan():
    """The planning builder reads only the just-drafted MissionSpecification's
    own summary fields."""
    class _FakeTask:
        def __init__(self, n):
            self.step_number = n
            self.action = "edit_file"
            self.target_file = f"file{n}.py"
            self.description = f"do thing {n}"

    class _FakeMission:
        outcome = "Ship the feature"
        constraints = ["no new deps"]
        tasks = [_FakeTask(1), _FakeTask(2)]

    request = build_planning_companion_request(
        session_id="sess1", task_id="task1", mission_plan=_FakeMission(),
    )
    assert request.scope == "planning"
    assert request.task_description == "Ship the feature"
    assert "file1.py" in request.scope_summary
    assert "file2.py" in request.scope_summary


def test_build_healing_companion_request_reflects_the_outcome():
    """The healing builder reports whether THIS attempt healed or conceded —
    not the accumulated error history."""
    healed = build_healing_companion_request(
        session_id="sess1", task_id="task1", attempt_ordinal=1,
        failed_node="coder", diagnosis="missing import", healed=True,
    )
    assert healed.scope == "healing"
    assert "healed" in healed.scope_summary

    conceded = build_healing_companion_request(
        session_id="sess1", task_id="task1", attempt_ordinal=2,
        failed_node="coder", diagnosis="unfixable schema drift", healed=False,
    )
    assert "could not auto-fix" in conceded.scope_summary


def test_build_healing_companion_request_carries_real_execution_context():
    """13.0.9: previously this request carried only three generic lines
    (failed_node/outcome/diagnosis) regardless of what actually failed — the
    model had nothing concrete to explain and fell back to filler. When the
    failure came from a run_command attempt, the caller (agents/error_correction.py)
    now threads the step description, the command, its exit code, and the
    stdout/stderr tail through."""
    request = build_healing_companion_request(
        session_id="sess1", task_id="task1", attempt_ordinal=1,
        failed_node="apply_commit", diagnosis="mypy.py:12: error: bad type [arg-type]",
        healed=False, step_description="Fix the type hints in calc.py",
        command="mypy .", exit_code=1,
        stdout_tail="calc.py:12: error: bad type [arg-type]", stderr_tail="",
    )
    assert "Step: Fix the type hints in calc.py" in request.scope_summary
    assert "Command: mypy ." in request.scope_summary
    assert "Exit code: 1" in request.scope_summary
    assert "Stdout (tail):" in request.scope_summary
    assert "calc.py:12: error: bad type" in request.scope_summary
    # An empty stderr tail is falsy — omitted rather than rendered as a bare header.
    assert "Stderr (tail):" not in request.scope_summary


def test_build_healing_companion_request_omits_execution_fields_for_a_non_command_failure():
    """A write_file/edit_file-originated failure has no command/exit_code/tail
    at all — the caller passes None for each, and none of those lines should
    render (never "Command: None", "Exit code: None", ...)."""
    request = build_healing_companion_request(
        session_id="sess1", task_id="task1", attempt_ordinal=1,
        failed_node="coder_agent", diagnosis="KeyError: 'foo'", healed=False,
    )
    assert "Command:" not in request.scope_summary
    assert "Exit code:" not in request.scope_summary
    assert "Stdout" not in request.scope_summary
    assert "Stderr" not in request.scope_summary
    assert "Step:" not in request.scope_summary


async def test_run_agent_companion_broadcasts_with_scope_and_emission_id(mock_state):
    """A non-coding emission carries its own scope + a per-decision-point
    emission_id, distinct from the coding-path correlation_id shape."""
    request = build_ideation_companion_request(
        session_id="test-task-123", task_id="test-task-123", attempt_ordinal=0,
        task_description="task", question_batch=[], resolved_answers={},
    )
    mock_analysis = CompanionAnalysis(objective="Explained the round", degraded=False)

    with patch("brain.coder_companion._ideation_companion_would_contend_local_compute", return_value=False):
        with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock, return_value=mock_analysis):
            with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
                with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock, return_value=True):
                    await _run_agent_companion(mock_state, "ideation", 0, lambda: request)

    mock_broadcast.assert_called_once()
    kwargs = mock_broadcast.call_args[1]
    assert kwargs["scope"] == "ideation"
    assert kwargs["emission_id"] == "test-task-123:ideation:0"


def test_ideation_local_contention_probe_true_for_local_judge_tier():
    """DEBT-190: the judge tier resolving to a local BYOM target means the
    grill's own next local call would contend with the companion's — the
    probe must say so."""
    with patch("core.config.model_resolver.get_chat_target", return_value=Mock(is_local=True)):
        assert _ideation_companion_would_contend_local_compute() is True


def test_ideation_local_contention_probe_false_for_cloud_judge_tier():
    with patch("core.config.model_resolver.get_chat_target", return_value=Mock(is_local=False)):
        assert _ideation_companion_would_contend_local_compute() is False


def test_ideation_local_contention_probe_false_when_unresolved():
    """No BYOM preset resolves this tier — nothing to contend with locally."""
    with patch("core.config.model_resolver.get_chat_target", return_value=None):
        assert _ideation_companion_would_contend_local_compute() is False


def test_ideation_local_contention_probe_fails_open_on_error():
    with patch("core.config.model_resolver.get_chat_target", side_effect=RuntimeError("boom")):
        assert _ideation_companion_would_contend_local_compute() is False


async def test_run_agent_companion_ideation_skips_cleanly_when_judge_tier_is_local(mock_state):
    """The actual bug this closes: on a local-only BYOM setup, every grill
    round's companion attempt queued behind the grill's own immediately-
    following local LLM call (Ollama serializes same-model requests) and
    reliably exceeded the 45s companion timeout, surfacing as 'N explanation(s)
    were unavailable' in the chat on every single round. The fix skips BEFORE
    attempting the call — no broadcast at all, not even a degraded one — since
    `_companion_gpu_slot_available`'s VRAM-lock probe can't see this: the
    grill's own local calls never register with that lock."""
    request = build_ideation_companion_request(
        session_id="test-task-123", task_id="test-task-123", attempt_ordinal=0,
        task_description="task", question_batch=[], resolved_answers={},
    )
    with patch("brain.coder_companion._ideation_companion_would_contend_local_compute", return_value=True):
        with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock) as mock_llm:
            with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
                await _run_agent_companion(mock_state, "ideation", 0, lambda: request)

    mock_llm.assert_not_called()
    mock_broadcast.assert_not_called()


async def test_run_agent_companion_ideation_proceeds_when_judge_tier_is_cloud(mock_state):
    """The skip is scoped to genuine local-vs-local contention — a cloud judge
    tier never contends for the same local inference slot, so it fires as before."""
    request = build_ideation_companion_request(
        session_id="test-task-123", task_id="test-task-123", attempt_ordinal=0,
        task_description="task", question_batch=[], resolved_answers={},
    )
    mock_analysis = CompanionAnalysis(objective="Explained the round", degraded=False)
    with patch("brain.coder_companion._ideation_companion_would_contend_local_compute", return_value=False):
        with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock, return_value=mock_analysis):
            with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
                with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock, return_value=True):
                    await _run_agent_companion(mock_state, "ideation", 0, lambda: request)

    mock_broadcast.assert_called_once()


async def test_run_coder_companion_unaffected_by_ideation_local_contention_gate(mock_state):
    """The coding-scope path never consults the new probe at all — the coder's
    own local generation always finishes (and releases the GPU lock) before
    schedule_coder_companion fires, so no such contention exists there."""
    with patch("brain.coder_companion._ideation_companion_would_contend_local_compute", return_value=True) as mock_probe:
        with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock,
                   return_value=CompanionAnalysis(objective="ok", degraded=False)):
            with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
                with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock, return_value=True):
                    await _run_coder_companion(mock_state, 0)

    mock_probe.assert_not_called()
    mock_broadcast.assert_called_once()


async def test_schedule_agent_companion_gc_safety(mock_state):
    """Mirrors schedule_coder_companion's GC-safety contract for the new
    non-coding entry point."""
    _companion_background_tasks.clear()
    request = build_healing_companion_request(
        session_id="test-task-123", task_id="test-task-123", attempt_ordinal=0,
        failed_node="coder", diagnosis="x", healed=True,
    )
    with patch("brain.coder_companion._run_agent_companion", new_callable=AsyncMock) as mock_run:
        schedule_agent_companion(mock_state, "healing", 0, lambda: request)
        assert len(_companion_background_tasks) == 1
        await asyncio.gather(*_companion_background_tasks)
        assert len(_companion_background_tasks) == 0
    mock_run.assert_called_once()


async def test_companion_emission_cap_stops_a_pathological_cycle(mock_state):
    """The shared per-task emission ceiling is a backstop against a cyclic
    decision-point emitter (e.g. a coder ↔ error_correction loop) — past the
    cap, no further broadcast fires for that task, regardless of scope."""
    request = build_healing_companion_request(
        session_id="test-task-123", task_id="test-task-123", attempt_ordinal=0,
        failed_node="coder", diagnosis="x", healed=False,
    )
    with patch("brain.coder_companion._call_analyst_llm", new_callable=AsyncMock,
               return_value=CompanionAnalysis(objective="ok", degraded=False)):
        with patch("api.websocket_manager.vfs_manager.broadcast_coder_companion", new_callable=AsyncMock) as mock_broadcast:
            with patch("brain.coder_companion._companion_gpu_slot_available", new_callable=AsyncMock, return_value=True):
                for _ in range(25):  # over the 20-emission ceiling
                    await _run_agent_companion(mock_state, "healing", 0, lambda: request)

    assert mock_broadcast.call_count == 20


# ─── JUDGE-TIER / TIMEOUT RESOLUTION TESTS ────────────────────────────────────

def test_resolve_judge_tier_defaults_medium():
    """A non-ailienant/ alias (or unrecognized suffix) resolves to 'medium'."""
    with patch("brain.coder_companion.MINI_JUDGE_MODEL", "gpt-4o-mini"):
        assert _resolve_judge_tier() == "medium"


def test_resolve_judge_tier_reads_alias_suffix():
    """An ailienant/<tier> alias resolves to its literal tier suffix."""
    with patch("brain.coder_companion.MINI_JUDGE_MODEL", "ailienant/small"):
        assert _resolve_judge_tier() == "small"


def test_resolve_companion_llm_timeout_local_vs_cloud():
    """A resolved local target delegates to the DEBT-191 calibrated/scaled timeout
    (not a flat guess); a remote one keeps the fixed cloud deadline."""
    local_target = Mock(is_local=True, model="ollama_chat/phi4")
    with patch("core.config.model_resolver.get_chat_target", return_value=local_target):
        assert _resolve_companion_llm_timeout("medium", 420) == resolve_local_timeout(420, "ollama_chat/phi4")

    remote_target = Mock(is_local=False)
    with patch("core.config.model_resolver.get_chat_target", return_value=remote_target):
        assert _resolve_companion_llm_timeout("medium", 420) == 12.0


def test_resolve_companion_llm_timeout_unresolved_alias_falls_back_to_cloud():
    """No active BYOM preset (unresolved alias) — advisory failure, cloud default."""
    with patch("core.config.model_resolver.get_chat_target", return_value=None):
        assert _resolve_companion_llm_timeout("medium", 420) == 12.0
