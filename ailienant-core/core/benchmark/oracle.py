"""Multi-file patch oracle and Resolve@k scorer.

The multi-file complement to the plain-codegen adapter. Where the codegen
adapter scores self-contained functions, this oracle scores candidate patches
against a frozen corpus of interconnected Python modules. A patch passes when
the corpus's own test suite exits zero after the patch is applied over a fresh
copy of the snapshot.

Security: every candidate patch is AST-scanned before any file is written.
Blocked patterns (dangerous imports, Level-1 reflexivity vectors) cause an
immediate failed Verdict without spawning a subprocess.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

from core.benchmark.codegen import Completion, Language, _FENCE_RE
from core.benchmark.executors import CodegenExecutor
from core.benchmark.hygiene import BenchmarkAbort

if TYPE_CHECKING:
    from core.indexer import LazyIndexer

_CORPUS_DIR = Path(__file__).parent / "corpus"
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_INDEXER_TIMEOUT_S = 300.0
_STDERR_CAP = 2000


# --------------------------------------------------------------------------- #
# Corpus problem schema                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class CorpusProblem:
    """One multi-file coding problem in the frozen corpus.

    ``golden_patch`` maps relative paths to their full replacement content;
    ``test_body`` is a standalone Python script whose assertions determine
    whether a candidate patch is correct.
    """

    task_id: str
    language: Language
    prompt: str
    target_files: Tuple[str, ...]
    golden_patch: Dict[str, str]
    test_body: str
    dependency_seed: str


@dataclass(frozen=True)
class Verdict:
    """Oracle outcome for one problem."""

    task_id: str
    passed: bool
    failures: Tuple[str, ...] = ()


@dataclass
class ResolveKReport:
    """Aggregate Resolve@k over the frozen corpus."""

    total: int
    resolved: int
    resolve_at_k: float
    indexing_time_s: float
    verdicts: List[Verdict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Corpus loader                                                                 #
# --------------------------------------------------------------------------- #


def load_corpus(name: str) -> Tuple[Path, List[CorpusProblem]]:
    """Parse a frozen corpus from the package ``corpus/`` directory.

    Returns ``(corpus_root, problems)``. The pinned SHA in ``meta.json`` is
    validated to be non-empty — its absence indicates a corrupt or incomplete
    fixture.
    """
    corpus_root = _CORPUS_DIR / name
    meta: Dict[str, Any] = json.loads(
        (corpus_root / "meta.json").read_text(encoding="utf-8")
    )
    if not meta.get("pinned_sha"):
        raise ValueError(
            f"corpus {name!r}: meta.json is missing a non-empty 'pinned_sha'"
        )

    problems: List[CorpusProblem] = []
    for line in (corpus_root / "problems.jsonl").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        record: Dict[str, Any] = json.loads(stripped)
        problems.append(
            CorpusProblem(
                task_id=str(record["task_id"]),
                language=Language.PYTHON,
                prompt=str(record["prompt"]),
                target_files=tuple(str(f) for f in record["target_files"]),
                golden_patch={str(k): str(v) for k, v in record["golden_patch"].items()},
                test_body=str(record["test_body"]),
                dependency_seed=str(record["dependency_seed"]),
            )
        )
    return corpus_root, problems


# --------------------------------------------------------------------------- #
# Multi-file patch extractor                                                    #
# --------------------------------------------------------------------------- #


def extract_patch(text: str, target_files: Tuple[str, ...]) -> Dict[str, str]:
    """Extract a ``{relative_path: content}`` patch from a model response.

    Primary: fenced blocks whose info string carries a filename
    (e.g. ``python src/math_utils.py``) are mapped by that filename.

    Fallback: if no tagged block is found and exactly **one** target file was
    requested, the first untagged block (or any block) is used. For multiple
    target files with no filename tags an empty dict is returned — guessing
    which file a block belongs to would silently corrupt an oracle result.
    """
    blocks: List[Tuple[str, str]] = [
        (tag.strip(), body) for tag, body in _FENCE_RE.findall(text)
    ]

    patch: Dict[str, str] = {}
    for tag, body in blocks:
        parts = tag.split(None, 1)
        if len(parts) == 2:
            patch[parts[1].strip()] = body.strip("\n")

    if patch:
        return patch

    if len(target_files) != 1:
        return {}

    target = target_files[0]
    bare = [body for tag, body in blocks if len(tag.split(None, 1)) < 2]
    if bare:
        return {target: bare[0].strip("\n")}
    if blocks:
        return {target: blocks[0][1].strip("\n")}
    return {}


# --------------------------------------------------------------------------- #
# Multi-file prompt builder                                                     #
# --------------------------------------------------------------------------- #


def build_corpus_messages(
    problem: CorpusProblem, corpus_root: Path
) -> List[Dict[str, str]]:
    """Build the chat messages that elicit a multi-file patch.

    The system instruction demands one fenced block per changed file with the
    relative path in the info string (``python src/math_utils.py``). This
    format is what ``extract_patch`` expects — no loose prose, no extra blocks.
    """
    system = (
        "You are patching a multi-file Python project. "
        "Return ONLY the changed files. Use one fenced code block per file "
        "with the relative path in the info string, e.g.: "
        "```python src/math_utils.py. "
        "Do not add explanations, prose, or blocks for unchanged files."
    )

    file_sections: List[str] = []
    for rel_path in problem.target_files:
        src = corpus_root / rel_path
        content = src.read_text(encoding="utf-8") if src.exists() else ""
        file_sections.append(f"### {rel_path}\n```python\n{content}```")

    user = "\n\n".join(file_sections) + f"\n\n### Task\n{problem.prompt}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- #
# AST safety validator                                                          #
# --------------------------------------------------------------------------- #

_BLOCKED_IMPORTS: FrozenSet[str] = frozenset(
    {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "urllib",
        "http",
        "pathlib",
        "ftplib",
        "requests",
        "multiprocessing",
        "threading",
        "ctypes",
        "signal",
        "importlib",
    }
)

# Level-1 reflexivity vectors included: getattr/setattr/__builtins__/vars/
# globals/locals allow an attacker to reconstruct blocked names from string
# fragments or via namespace lookup.
_BLOCKED_BUILTINS: FrozenSet[str] = frozenset(
    {
        "eval",
        "exec",
        "__import__",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "__builtins__",
        "vars",
        "globals",
        "locals",
    }
)


def _check_patch_safety(content: str, path: str) -> Optional[str]:
    """Return None if the Python source is safe, or a description of the first
    blocked pattern found.

    Checks for dangerous module imports and Level-1 reflexivity builtins that
    could bypass the import blocklist. Called on every file in a candidate patch
    before any temp file is written.
    """
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_IMPORTS:
                    return f"blocked import: {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _BLOCKED_IMPORTS:
                return f"blocked import from: {node.module!r}"
        elif isinstance(node, ast.Call):
            func = node.func
            name: Optional[str] = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in _BLOCKED_BUILTINS:
                return f"blocked builtin call: {name!r}"

    return None


# --------------------------------------------------------------------------- #
# Oracle                                                                        #
# --------------------------------------------------------------------------- #


class BenchmarkOracle:
    """Applies a candidate patch over the frozen snapshot and runs its tests.

    ``corpus_root`` must be the directory that contains ``src/`` and
    ``problems.jsonl``. ``executor`` is injected so the hermetic gate can
    substitute a host subprocess without touching this class.
    """

    def __init__(self, corpus_root: Path, executor: CodegenExecutor) -> None:
        self._corpus_root = corpus_root
        self._executor = executor

    async def run_oracle(
        self,
        problem: CorpusProblem,
        candidate_patch: Dict[str, str],
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> Verdict:
        """Apply ``candidate_patch`` and return the verdict.

        Steps: (1) AST safety pre-flight, (2) delegate to the executor, which
        materializes the corpus snapshot plus the patch inside its own isolation
        envelope (a Docker container for live output, a reaped host subprocess
        for the trusted hermetic gate) and runs the problem's test program.
        """
        # 1. Safety pre-flight — no workspace is materialized on rejection.
        for rel_path, content in candidate_patch.items():
            reason = _check_patch_safety(content, rel_path)
            if reason is not None:
                return Verdict(
                    task_id=problem.task_id,
                    passed=False,
                    failures=(f"[safety_blocked: {rel_path}: {reason}]",),
                )

        # 2. Execute inside the isolation envelope. The executor owns workspace
        # materialization, the lexical path-traversal guard, and cleanup.
        outcome = await self._executor.run_workspace(
            corpus_src=self._corpus_root / "src",
            patch=candidate_patch,
            test_body=problem.test_body,
            language=Language.PYTHON,
            timeout_s=timeout_s,
        )

        failures = tuple(
            line
            for line in outcome.stderr[:_STDERR_CAP].splitlines()
            if "Error" in line or "error" in line or "assert" in line.lower()
        )[:10]

        return Verdict(
            task_id=problem.task_id,
            passed=outcome.passed,
            failures=failures,
        )


# --------------------------------------------------------------------------- #
# Indexer helpers                                                               #
# --------------------------------------------------------------------------- #


async def _await_index(
    corpus_root: Path,
    project_id: str,
    indexer: "LazyIndexer",
    timeout_s: float,
) -> float:
    """Start the indexer and block until it completes.

    Accesses ``indexer.complete_event`` *before* calling ``start()`` so the
    event object exists before ``_run()`` can fire it. After ``start()`` the
    ``is_complete`` flag is checked immediately — if the indexer recognised the
    corpus as already indexed (crash-resume), ``_run()`` is never scheduled and
    the event will not fire, so we return early with ``0.0``.
    """
    event = indexer.complete_event
    await indexer.start(str(corpus_root), project_id, session_id=uuid.uuid4().hex)
    if indexer.is_complete:
        return 0.0
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise BenchmarkAbort(
            f"indexer did not complete within {timeout_s:.0f}s for corpus {corpus_root.name!r}"
        )
    return time.perf_counter() - t0


async def _assert_dependents_nonempty(seed: str, project_id: str) -> None:
    """Abort if the dependency graph returned no dependents for the seed file.

    An empty result means the index did not capture the file's edges — the
    GraphRAG arms would see zero graph context, making the G2/G3/G4 vs G1
    delta meaningless.
    """
    from core.db import get_dependents

    deps = await get_dependents(seed, project_id)
    if not deps:
        raise BenchmarkAbort(
            f"get_dependents({seed!r}, {project_id!r}) returned empty — "
            "corpus was not fully indexed or the dependency edge is missing"
        )


# --------------------------------------------------------------------------- #
# Resolve@k orchestrator                                                        #
# --------------------------------------------------------------------------- #


async def evaluate_resolve_k(
    problems: List[CorpusProblem],
    *,
    corpus_root: Path,
    completion: Completion,
    oracle: BenchmarkOracle,
    project_id: str = "",
    indexer_timeout_s: float = _DEFAULT_INDEXER_TIMEOUT_S,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ResolveKReport:
    """Score Resolve@k: one generation per problem, executed by the oracle.

    The indexer is awaited before the first problem runs. If the dependency
    graph is empty after indexing, the run aborts — a zero-graph run would
    measure noise, not routing value.

    Runs serially for determinism (mirrors the ablation runner).
    """
    from core.indexer import LazyIndexer

    effective_id = project_id or f"benchmark_corpus_{corpus_root.name}"
    indexer = LazyIndexer()
    indexing_time_s = await _await_index(
        corpus_root, effective_id, indexer, indexer_timeout_s
    )
    await _assert_dependents_nonempty(problems[0].dependency_seed, effective_id)

    verdicts: List[Verdict] = []
    for problem in problems:
        messages = build_corpus_messages(problem, corpus_root)
        raw = await completion(messages)
        patch = extract_patch(raw, problem.target_files)
        verdict = await oracle.run_oracle(problem, patch, timeout_s)
        verdicts.append(verdict)

    resolved = sum(1 for v in verdicts if v.passed)
    total = len(verdicts)
    return ResolveKReport(
        total=total,
        resolved=resolved,
        resolve_at_k=(resolved / total) if total else 0.0,
        indexing_time_s=indexing_time_s,
        verdicts=verdicts,
    )
