"""Polyglot dependency extraction: the ``IMPORT_EXTRACTORS`` registry.

Exercises the language-dispatched import-edge extraction directly against real
tree-sitter grammars (Python + TypeScript/JavaScript), plus the confidence
resolver's extension/``index.*`` candidate expansion:
- Python extraction is unchanged from the pre-registry behaviour (absolute module
  paths only; relative imports skipped),
- TS/JS captures static imports, re-exports, dynamic ``import()`` and ``require()``,
- relative TS/JS specifiers resolve lexically (disk-free, OS-agnostic) and are
  workspace-confined; directory escapes (parent and sibling-prefix) are dropped,
- a resolved relative target reaches EXTRACTED via candidate expansion (including a
  ``dir/index.ts`` barrel); a bare specifier stays INFERRED,
- dispatch is dict-based (O(1)); an unregistered language yields no edges.
"""
from __future__ import annotations

import os
from typing import Any, List
from unittest.mock import Mock

import pytest

import brain.memory as memory
from brain.memory import (
    IMPORT_EXTRACTORS,
    _extract_ecmascript_imports,
    _resolve_edge_confidence,
    index_file_sync,
)
from core.ast_engine import ASTEngine
from shared.contracts import IndexingRequest


def _parse(content: str, language_id: str, path: str = "/ws/src/a.ts") -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str = "/ws/src/a.ts", workspace_root: str = "/ws") -> IndexingRequest:
    return IndexingRequest(
        file_path=path, content="", language_id="typescript", workspace_root=workspace_root
    )


# ── Python parity ────────────────────────────────────────────────────────────


def test_python_extraction_absolute_only_relative_skipped() -> None:
    src = (
        "import os\n"
        "import sys as system\n"
        "from collections import Counter\n"
        "from . import relative_thing\n"
        "from .mod import x\n"
        "import a.b.c\n"
        "from pkg.sub import y\n"
    )
    tree = _parse(src, "python", path="/ws/pkg/m.py")
    req = IndexingRequest(
        file_path="/ws/pkg/m.py", content=src, language_id="python", workspace_root="/ws"
    )
    imports = IMPORT_EXTRACTORS["python"](tree, req)
    # Absolute module paths only; the two relative ("from .") forms are skipped.
    assert imports == ["os", "sys", "collections", "a.b.c", "pkg.sub"]


# ── TypeScript / JavaScript extraction ───────────────────────────────────────


def test_ts_static_reexport_dynamic_require_all_captured() -> None:
    src = (
        "import a from './a';\n"
        "import { b } from '../pkg/b';\n"
        "export { d } from './d';\n"
        "export * from './e';\n"
        "const x = import('./dyn');\n"
        "const y = require('./req');\n"
        "async function f() { return import('../lazy/mod'); }\n"
    )
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == [
        "/ws/src/a",
        "/ws/pkg/b",
        "/ws/src/d",
        "/ws/src/e",
        "/ws/src/dyn",
        "/ws/src/req",
        "/ws/lazy/mod",
    ]


def test_js_grammar_matches_ts_behaviour() -> None:
    src = "import a from './a';\nconst y = require('./req');\n"
    tree = _parse(src, "javascript", path="/ws/src/a.js")
    imports = _extract_ecmascript_imports(tree, _req(path="/ws/src/a.js"))
    assert imports == ["/ws/src/a", "/ws/src/req"]


def test_ts_bare_specifier_emitted_verbatim() -> None:
    src = "import * as React from 'react';\nimport { z } from '@scope/pkg';\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == ["react", "@scope/pkg"]


def test_ts_duplicate_specifier_deduped() -> None:
    src = "import a from './a';\nconst lazy = import('./a');\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == ["/ws/src/a"]


def test_ts_template_and_computed_specifiers_skipped() -> None:
    # Template/computed dynamic-import args are non-lexical and must be skipped,
    # while a plain static import in the same file is still captured.
    src = "import a from './a';\nconst v = 'x';\nconst d = import(`./${v}`);\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == ["/ws/src/a"]


# ── Workspace confinement (path-traversal containment) ───────────────────────


def test_ts_parent_escape_dropped() -> None:
    src = "import p from '../../../etc/passwd';\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == []


def test_ts_sibling_prefix_escape_dropped() -> None:
    # '/ws/src' + '../../ws_hacked/x' -> '/ws_hacked/x', which shares the textual
    # prefix '/ws' but is NOT under the '/ws/' directory boundary.
    src = "import p from '../../ws_hacked/x';\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req())
    assert imports == []


def test_ts_empty_workspace_root_resolves_without_guard() -> None:
    src = "import a from './a';\n"
    tree = _parse(src, "typescript")
    imports = _extract_ecmascript_imports(tree, _req(workspace_root=""))
    assert imports == ["/ws/src/a"]


def test_windows_origin_path_resolves_os_agnostically() -> None:
    # A Windows-origin path must resolve identically on any worker OS: force
    # forward-slashes + posixpath, never host os.path.
    src = "import b from '../lib/b';\n"
    tree = _parse(src, "typescript", path="C:\\ws\\src\\a.ts")
    req = IndexingRequest(
        file_path="C:\\ws\\src\\a.ts",
        content=src,
        language_id="typescript",
        workspace_root="C:\\ws",
    )
    imports = _extract_ecmascript_imports(tree, req)
    assert imports == ["C:/ws/lib/b"]


# ── Confidence resolver: candidate expansion ─────────────────────────────────


def test_resolver_relative_target_reaches_extracted() -> None:
    edges = (
        ("/ws/src/main.ts", "/ws/src/a"),
        ("/ws/src/main.ts", "/ws/src/widgets"),  # resolves via dir/index.ts
    )
    indexed = ("/ws/src/a.ts", "/ws/src/widgets/index.ts")
    out = _resolve_edge_confidence(edges, indexed)
    assert out == (
        ("/ws/src/main.ts", "/ws/src/a.ts", "EXTRACTED", 1.0),
        ("/ws/src/main.ts", "/ws/src/widgets/index.ts", "EXTRACTED", 1.0),
    )


def test_resolver_bare_specifier_inferred() -> None:
    out = _resolve_edge_confidence(
        (("/ws/src/main.ts", "react"),), ("/ws/src/a.ts",)
    )
    assert out == (("/ws/src/main.ts", "react", "INFERRED", 0.5),)


# ── Registry contract: filesystem-free, dispatch, unregistered languages ─────


def test_extraction_and_resolution_are_filesystem_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    src = "import a from './a';\nconst y = require('../lib/b');\n"
    tree = _parse(src, "typescript")
    _extract_ecmascript_imports(tree, _req())
    _resolve_edge_confidence(
        (("/ws/src/main.ts", "/ws/src/a"),), ("/ws/src/a.ts",)
    )
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


def test_unregistered_language_yields_no_edges() -> None:
    assert IMPORT_EXTRACTORS.get("go") is None
    req = IndexingRequest(
        file_path="/ws/main.go",
        content="package main\nimport \"fmt\"\n",
        language_id="go",
        workspace_root="/ws",
    )
    result = index_file_sync(req)
    assert result.success is True
    assert result.imports == []


def test_dispatch_is_registry_based(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel: List[str] = ["SENTINEL"]
    called: dict[str, Any] = {}

    def _sentinel_extractor(tree: Any, req: IndexingRequest) -> List[str]:
        called["req"] = req
        return sentinel

    monkeypatch.setitem(IMPORT_EXTRACTORS, "python", _sentinel_extractor)
    req = IndexingRequest(
        file_path="/ws/m.py", content="import os\n", language_id="python", workspace_root="/ws"
    )
    result = index_file_sync(req)
    assert result.imports == sentinel
    assert called["req"] is req
