"""Hermetic gate for the codegen Pass@1 adapter.

No live model, no Docker: a scripted completion feeds known generations and a
host subprocess executes the trusted result, so the gate proves the scorer
genuinely runs the unit tests (a wrong solution must score 0), the result is
reproducible, the temperature is pinned, the extractor survives multi-block
replies, the subprocess reaper fires on a hang, and TypeScript is reported
unsupported without reaching Docker.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List

import pytest

from tests.benchmark.codegen import (
    CodegenProblem,
    Language,
    Pass1Report,
    assemble_program,
    build_messages,
    default_completion,
    evaluate_pass1,
    extract_code,
    load_dataset,
)
from tests.benchmark.executors import SandboxCodegenExecutor, SubprocessPythonExecutor
from tests.benchmark.hygiene import TEMPERATURE

_Completion = Callable[[List[Dict[str, str]]], Awaitable[str]]


def _scripted(responses: List[str]) -> _Completion:
    """A completion that returns canned responses in call order.

    ``evaluate_pass1`` processes problems serially, so responses built in the
    dataset's order line up one-to-one with the problems.
    """
    iterator = iter(responses)

    async def _complete(_messages: List[Dict[str, str]]) -> str:
        return next(iterator)

    return _complete


def _fenced(problem: CodegenProblem, body: str) -> str:
    """Wrap a full function (stub + body) in a python code block."""
    return f"```python\n{problem.prompt}{body}```"


# --- Pass@1 over the frozen subset -------------------------------------------


def test_pass1_perfect_on_correct_completion() -> None:
    problems = load_dataset("humaneval_subset.jsonl")
    responses = [_fenced(p, p.canonical_solution) for p in problems]
    report = asyncio.run(
        evaluate_pass1(
            problems,
            completion=_scripted(responses),
            executor=SubprocessPythonExecutor(),
        )
    )
    assert report.total == 3
    assert report.passed == 3
    assert report.pass_at_1 == 1.0
    assert report.per_language["py"] == (3, 3)


def test_pass1_zero_on_wrong_completion() -> None:
    problems = load_dataset("humaneval_subset.jsonl")
    # A body that type-checks and runs but fails every assertion.
    responses = [_fenced(p, "    return 999\n") for p in problems]
    report = asyncio.run(
        evaluate_pass1(
            problems,
            completion=_scripted(responses),
            executor=SubprocessPythonExecutor(),
        )
    )
    assert report.passed == 0
    assert report.pass_at_1 == 0.0
    # Proof the oracle truly ran the tests: a non-zero exit, not a skipped run.
    assert all(v.exit_code != 0 for v in report.verdicts)


def test_pass1_reproducible() -> None:
    problems = load_dataset("humaneval_subset.jsonl")

    def _run() -> Pass1Report:
        responses = [_fenced(p, p.canonical_solution) for p in problems]
        return asyncio.run(
            evaluate_pass1(
                problems,
                completion=_scripted(responses),
                executor=SubprocessPythonExecutor(),
            )
        )

    first, second = _run(), _run()
    assert (first.total, first.passed, first.pass_at_1) == (
        second.total,
        second.passed,
        second.pass_at_1,
    )
    assert first.per_language == second.per_language


# --- extraction --------------------------------------------------------------


def test_extract_code_handles_fenced_raw_and_multiblock() -> None:
    # Single tagged block.
    assert "def f" in extract_code("```python\ndef f():\n    return 1\n```", Language.PYTHON)
    # TypeScript tag.
    assert "function f" in extract_code(
        "```typescript\nfunction f() { return 1; }\n```", Language.TYPESCRIPT
    )
    # No fences — raw fallback.
    assert "def g" in extract_code("def g():\n    return 2", Language.PYTHON)
    # Pseudocode block first, real function second — both concatenated.
    multi = (
        "```python\n# pseudocode: return the sum\n```\n"
        "```python\ndef add(a, b):\n    return a + b\n```"
    )
    extracted = extract_code(multi, Language.PYTHON)
    assert "pseudocode" in extracted and "def add" in extracted


# --- temperature pinning -----------------------------------------------------


def test_default_completion_pins_temperature_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, object] = {}

    class _Msg:
        content = "```python\ndef f():\n    return 1\n```"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def _fake_ainvoke(**kwargs: object) -> _Resp:
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr("tools.llm_gateway.LLMGateway.ainvoke", _fake_ainvoke)

    completion = default_completion(model="ailienant/big")

    async def _drive() -> str:
        return await completion([{"role": "user", "content": "x"}])

    asyncio.run(_drive())
    assert captured["temperature"] == TEMPERATURE


# --- executor edges ----------------------------------------------------------


def test_typescript_runtime_unsupported_in_sandbox() -> None:
    # The TS branch returns before any tier/Docker work, so this is hermetic.
    outcome = asyncio.run(
        SandboxCodegenExecutor().run("function f() {}", Language.TYPESCRIPT, 5.0)
    )
    assert outcome.passed is False
    assert "unsupported_runtime" in outcome.stderr


def test_subprocess_timeout_reaps_child() -> None:
    outcome = asyncio.run(
        SubprocessPythonExecutor().run(
            "import time\ntime.sleep(30)\n", Language.PYTHON, 1.0
        )
    )
    assert outcome.passed is False
    assert outcome.exit_code == -1
    assert "subprocess_timeout" in outcome.stderr


# --- dataset loading ---------------------------------------------------------


def test_loads_frozen_subset() -> None:
    py = load_dataset("humaneval_subset.jsonl")
    ts = load_dataset("multipl_e_ts_subset.jsonl")

    assert len(py) == 3
    assert all(p.language is Language.PYTHON for p in py)
    assert {p.entry_point for p in py} == {"add", "is_even", "reverse_string"}
    # The 'test' key maps onto tests; canonical_solution is carried for the oracle.
    assert py[0].tests and py[0].canonical_solution

    assert len(ts) == 2
    assert all(p.language is Language.TYPESCRIPT for p in ts)
    assert ts[0].stop_tokens  # stop_tokens parsed from the fixture

    # assemble_program uses the extracted full function, not a re-prepended stub.
    program = assemble_program(py[0], py[0].prompt + py[0].canonical_solution)
    assert program.count("def add") == 1
    assert "check(add)" in program
    # build_messages demands a single fenced block with no extra prose.
    messages = build_messages(py[0])
    assert messages[0]["role"] == "system" and "single" in messages[0]["content"]
