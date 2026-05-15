# ailienant-core/tests/test_parser_stress.py
# Phase 2.25.1 — Parser latency benchmark for _sanitize_json_response.
import json
import time

from tools.llm_gateway import LLMGateway

# Shared metrics dict populated by tests; read by conftest.pytest_sessionfinish.
_PARSER_METRICS: dict = {}


def _make_mission_json(n_tasks: int = 10) -> str:
    data = {
        "outcome": "Refactor authentication module.",
        "scope": ["src/auth.py"],
        "constraints": ["No external libraries."],
        "decisions": ["Use bcrypt."],
        "tasks": [
            {
                "step_number": i,
                "target_role": "Refactor",
                "action": "edit_file",
                "target_file": f"src/module_{i}.py",
                "description": "Edit file.",
                "status": "pending",
            }
            for i in range(n_tasks)
        ],
        "checks": ["All tests pass."],
    }
    return json.dumps(data)


def test_parser_raw_json_no_overhead():
    """Raw JSON (no fence) passes through in < 50ms."""
    payload = _make_mission_json()
    start = time.perf_counter()
    result = LLMGateway._sanitize_json_response(payload)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _PARSER_METRICS["no_fence_ms"] = round(elapsed_ms, 3)
    assert elapsed_ms < 50
    assert json.loads(result)


def test_parser_fenced_json_extracted_correctly():
    """Fenced JSON is extracted and valid in < 50ms."""
    payload = _make_mission_json()
    wrapped = f"```json\n{payload}\n```"
    start = time.perf_counter()
    result = LLMGateway._sanitize_json_response(wrapped)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _PARSER_METRICS["fenced_ms"] = round(elapsed_ms, 3)
    assert elapsed_ms < 50
    assert json.loads(result)


def test_parser_large_payload_with_whitespace_noise():
    """2KB of leading/trailing whitespace + blank-line noise stays under 50ms."""
    payload = _make_mission_json(n_tasks=50)  # ~2KB JSON
    noise = "\n" * 40 + " " * 60
    wrapped = f"{noise}```json\n{payload}\n```{noise}"
    start = time.perf_counter()
    result = LLMGateway._sanitize_json_response(wrapped)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _PARSER_METRICS["large_noise_ms"] = round(elapsed_ms, 3)
    assert elapsed_ms < 50
    parsed = json.loads(result)
    assert len(parsed["tasks"]) == 50


def test_parser_malformed_fence_no_crash():
    """Unclosed fence returns raw stripped content — no crash, no data loss."""
    raw = '{"outcome": "test", "tasks": []}'
    malformed = f"```json\n{raw}"  # missing closing ```
    result = LLMGateway._sanitize_json_response(malformed)
    assert "outcome" in result
    _PARSER_METRICS["malformed_fence_ok"] = True
