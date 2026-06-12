"""Plain code-generation adapter and Pass@1 scorer.

This is the *model-alone* leg of the precision study: it loads a frozen subset
of self-contained coding problems (HumanEval-style Python, MultiPL-E-style
TypeScript), asks the model for a single generation at temperature 0, executes
the generation against the problem's unit tests, and reports Pass@1.

The problems are self-contained single functions, so a direct gateway
completion is the right baseline — there is no repository context for retrieval
to add. Execution is delegated to a pluggable :class:`CodegenExecutor` so the
isolation backend (a sandbox for live model output, a subprocess for the
trusted hermetic gate) is a swap-in, not baked into the scorer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from tests.benchmark.executors import CodegenExecutor
from tests.benchmark.hygiene import TEMPERATURE

_DATASETS_DIR = Path(__file__).parent / "datasets"
_DEFAULT_MODEL = "ailienant/big"
_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TIMEOUT_S = 30.0
_STDERR_CAP = 2000  # keep a verdict's stderr bounded in the report


class Language(str, Enum):
    """The generation target languages the adapter supports."""

    PYTHON = "py"
    TYPESCRIPT = "ts"


# Tags a fenced block may carry for each language. Used by the extractor to
# decide which blocks belong to the requested language.
_LANGUAGE_FENCE_TAGS: Dict[Language, frozenset[str]] = {
    Language.PYTHON: frozenset({"python", "py", "python3"}),
    Language.TYPESCRIPT: frozenset({"typescript", "ts", "tsx"}),
}


def _coerce_language(raw: str) -> Language:
    """Map a fixture's language string onto the enum (defaults to Python)."""
    token = (raw or "py").strip().lower()
    for lang in Language:
        if token == lang.value or token in _LANGUAGE_FENCE_TAGS[lang]:
            return lang
    return Language.PYTHON


@dataclass(frozen=True)
class CodegenProblem:
    """A single self-contained codegen problem and its reference oracle.

    ``prompt`` is the function stub shown to the model; ``canonical_solution``
    is the reference body that completes it (kept for the oracle / fixtures, not
    fed to the model); ``tests`` is the check suite; ``entry_point`` is the
    function name the suite exercises.
    """

    task_id: str
    language: Language
    prompt: str
    tests: str
    entry_point: str
    canonical_solution: str = ""
    stop_tokens: Tuple[str, ...] = ()


def load_dataset(name: str) -> List[CodegenProblem]:
    """Parse a frozen JSONL fixture from the package ``datasets/`` directory.

    The path is resolved relative to this module, never the working directory,
    so the loader behaves identically regardless of where pytest is invoked.
    Tolerates both the canonical ``test`` key and the ``tests`` key.
    """
    path = _DATASETS_DIR / name
    problems: List[CodegenProblem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        record: Dict[str, Any] = json.loads(stripped)
        tests = record.get("tests")
        if tests is None:
            tests = record.get("test", "")
        problems.append(
            CodegenProblem(
                task_id=str(record["task_id"]),
                language=_coerce_language(str(record.get("language", "py"))),
                prompt=str(record["prompt"]),
                tests=str(tests),
                entry_point=str(record["entry_point"]),
                canonical_solution=str(record.get("canonical_solution", "")),
                stop_tokens=tuple(record.get("stop_tokens", []) or []),
            )
        )
    return problems


def build_messages(problem: CodegenProblem) -> List[Dict[str, str]]:
    """Build the chat messages that elicit one self-contained generation.

    The instruction demands the *complete* function (signature included) in a
    single fenced block with no prose and no second block. That discipline keeps
    the extractor unambiguous and means the assembled program uses the returned
    block directly without re-prepending the stub.
    """
    language_name = "Python" if problem.language is Language.PYTHON else "TypeScript"
    fence = "python" if problem.language is Language.PYTHON else "typescript"
    system = (
        "You are an expert software engineer. Complete the requested function. "
        "Return ONLY the complete function definition, including its signature, "
        f"inside a single ```{fence} code block. Do not add explanations, prose, "
        "comments outside the function, or any second code block."
    )
    user = (
        f"Complete this {language_name} function. Keep the signature and name "
        f"unchanged.\n\n```{fence}\n{problem.prompt}```"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def extract_code(text: str, language: Language) -> str:
    """Pull runnable source out of a model response.

    Robust to a model that emits more than one block (e.g. a pseudocode sketch
    before the real answer): all blocks tagged for the requested language are
    concatenated in order. Falls back to untagged blocks, then to any block,
    then to the raw text with stray fences stripped — so a fence-less reply
    still yields something runnable.
    """
    blocks: List[Tuple[str, str]] = [
        (tag.strip().lower(), body) for tag, body in _FENCE_RE.findall(text)
    ]
    tags = _LANGUAGE_FENCE_TAGS[language]

    matching = [body for tag, body in blocks if tag in tags]
    if matching:
        return "\n".join(part.strip("\n") for part in matching)

    untagged = [body for tag, body in blocks if tag == ""]
    if untagged:
        return "\n".join(part.strip("\n") for part in untagged)

    if blocks:
        return "\n".join(body.strip("\n") for _, body in blocks)

    # No fences at all — strip any dangling backticks defensively.
    return text.replace("```", "").strip()


def assemble_program(problem: CodegenProblem, extracted: str) -> str:
    """Combine the generated function with the problem's test suite.

    ``extracted`` is the complete function, so the stub is not re-prepended.
    Python invokes ``check(entry_point)``; the TypeScript suites are
    self-invoking.
    """
    body = extracted.rstrip("\n")
    if problem.language is Language.PYTHON:
        return f"{body}\n\n{problem.tests}\ncheck({problem.entry_point})\n"
    return f"{body}\n\n{problem.tests}\n"


# A completion turns chat messages into a single raw model response string.
Completion = Callable[[List[Dict[str, str]]], Awaitable[str]]


def default_completion(
    model: str = _DEFAULT_MODEL, max_tokens: int = _DEFAULT_MAX_TOKENS
) -> Completion:
    """Build the live completion: one gateway call pinned to temperature 0.

    Pinning the temperature here is the determinism control for the model-alone
    path — there is deliberately no global temperature override inside the
    production code, which would amount to a benchmark flag in the hot path.
    """

    async def _complete(messages: List[Dict[str, str]]) -> str:
        from tools.llm_gateway import LLMGateway

        response = await LLMGateway.ainvoke(
            messages=list(messages),
            model=model,
            temperature=TEMPERATURE,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    return _complete


@dataclass(frozen=True)
class ProblemVerdict:
    """The per-problem outcome that aggregates into Pass@1."""

    task_id: str
    language: str
    passed: bool
    exit_code: int
    stderr: str


@dataclass
class Pass1Report:
    """Aggregate Pass@1 over a problem set, with a per-language breakdown."""

    total: int
    passed: int
    pass_at_1: float
    per_language: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    verdicts: List[ProblemVerdict] = field(default_factory=list)


async def evaluate_pass1(
    problems: List[CodegenProblem],
    *,
    completion: Completion,
    executor: CodegenExecutor,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Pass1Report:
    """Score Pass@1: one generation per problem, executed against its tests.

    Runs serially for determinism (mirrors the ablation runner). A problem
    passes when its assembled program exits zero.
    """
    verdicts: List[ProblemVerdict] = []
    per_language: Dict[str, List[int]] = {}

    for problem in problems:
        messages = build_messages(problem)
        raw = await completion(messages)
        extracted = extract_code(raw, problem.language)
        program = assemble_program(problem, extracted)
        outcome = await executor.run(program, problem.language, timeout_s)

        verdicts.append(
            ProblemVerdict(
                task_id=problem.task_id,
                language=problem.language.value,
                passed=outcome.passed,
                exit_code=outcome.exit_code,
                stderr=outcome.stderr[:_STDERR_CAP],
            )
        )
        bucket = per_language.setdefault(problem.language.value, [0, 0])
        bucket[1] += 1
        if outcome.passed:
            bucket[0] += 1

    total = len(verdicts)
    passed = sum(1 for verdict in verdicts if verdict.passed)
    return Pass1Report(
        total=total,
        passed=passed,
        pass_at_1=(passed / total) if total else 0.0,
        per_language={lang: (p, t) for lang, (p, t) in per_language.items()},
        verdicts=verdicts,
    )
