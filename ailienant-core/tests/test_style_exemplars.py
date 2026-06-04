# ailienant-core/tests/test_style_exemplars.py
"""AST-skeleton code-STYLE Few-Shot: distillation + prompt-block assembly.

Verifies that workspace functions are distilled to signature-only skeletons
(body elided), filtered to the target file's language, and framed distinctly
from the topology GraphRAG block — all best-effort (never raises)."""
from __future__ import annotations

from agents.coder import _build_style_block, _build_rag_block
from agents.prompts import STYLE_EXEMPLAR_HEADER
from core.ast_engine import _SKELETON_MAX_BYTES, extract_skeleton

_PY_FUNC = (
    "def greet(name: str) -> str:\n"
    '    """Return a friendly greeting."""\n'
    '    secret_body_token = "computed " + name\n'
    "    return secret_body_token\n"
)


def test_extract_skeleton_python_keeps_signature_drops_body() -> None:
    """DoD core: signature + type hints + docstring survive; body is elided."""
    skeleton = extract_skeleton(_PY_FUNC, "python")

    assert "def greet(name: str) -> str:" in skeleton  # signature + type hints
    assert '"""Return a friendly greeting."""' in skeleton  # docstring retained
    assert "..." in skeleton  # body placeholder
    assert "secret_body_token" not in skeleton  # body statements gone
    assert len(skeleton) < len(_PY_FUNC)  # materially smaller than source


def test_build_style_block_happy_path() -> None:
    """DoD: a same-language project gets the header + ≥1 elided skeleton."""
    block = _build_style_block("agents/widget.py", [("agents/helper.py", _PY_FUNC)])

    assert block.startswith(STYLE_EXEMPLAR_HEADER)
    assert "def greet(name: str) -> str:" in block
    assert "secret_body_token" not in block


def test_build_style_block_same_language_filter() -> None:
    """Only exemplars whose language matches the target file are admitted."""
    ts_snippet = "function tsOnlyFn(x: number): number { return x + 1; }"
    go_snippet = "func GoOnlyFn(x int) int { return x + 1 }"
    block = _build_style_block(
        "service.py",
        [
            ("helper.py", _PY_FUNC),
            ("widget.ts", ts_snippet),
            ("server.go", go_snippet),
        ],
    )

    assert "def greet" in block
    assert "tsOnlyFn" not in block
    assert "GoOnlyFn" not in block


def test_build_style_block_empty_and_exotic_return_blank() -> None:
    """No pairs, unsupported target extension, or no same-language match → ''."""
    assert _build_style_block("service.py", []) == ""
    assert _build_style_block("config.unknownext", [("helper.py", _PY_FUNC)]) == ""
    assert _build_style_block("service.py", [("notes.txt", "just prose")]) == ""


def test_extract_skeleton_exotic_language_returns_blank() -> None:
    """Unsupported language or non-code text degrades to '' without raising."""
    assert extract_skeleton(_PY_FUNC, "cobol") == ""
    assert extract_skeleton("@@@ not real code @@@", "python") == ""
    assert extract_skeleton("", "python") == ""


def test_extract_skeleton_truncated_snippet_never_raises() -> None:
    """Risk 1: a snippet cut mid-body (no closing 'body') must not raise."""
    # Bare signature (tree-sitter still yields an error-tolerant body node): the
    # only contract is that distillation never raises and returns a str.
    assert isinstance(extract_skeleton("def foo(x: int) -> str:", "python"), str)

    # Function whose body is sliced at the 500-char ingest cap.
    long_fn = (
        "def big(values: list[int]) -> int:\n"
        '    """Sum with a deliberately long body."""\n'
        + "".join(f"    acc_{i} = {i} * 2\n" for i in range(80))
    )
    truncated = long_fn[:500]
    result = extract_skeleton(truncated, "python")
    assert isinstance(result, str)  # the only contract: no exception


def test_style_block_distinct_from_topology_block() -> None:
    """The two blocks must not share a header (no merge/confusion)."""
    style = _build_style_block("helper.py", [("helper.py", _PY_FUNC)])
    topology = _build_rag_block([("helper.py", _PY_FUNC)])

    assert "House style exemplars" in style
    assert "House style exemplars" not in topology
    assert "GraphRAG" in topology
    assert "GraphRAG" not in style


def test_extract_skeleton_respects_byte_cap() -> None:
    """Many large functions → output is hard-capped for token safety."""
    many = "\n".join(
        f"def fn_{i}(argument_{i}: int, other_{i}: str) -> dict[str, int]:\n"
        f'    """Docstring number {i} describing the behaviour."""\n'
        f"    return {{'value': argument_{i}}}\n"
        for i in range(200)
    )
    skeleton = extract_skeleton(many, "python")
    assert len(skeleton.encode("utf-8")) <= _SKELETON_MAX_BYTES
