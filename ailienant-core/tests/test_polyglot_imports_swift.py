"""Polyglot import extraction — Swift.

Isolated from the other families because Swift's contract is genuinely different:
extraction succeeds trivially, but resolution to a local project file almost never
does, BY DESIGN — Swift's implicit whole-module visibility means files within the
same module never explicitly import each other, so ``import Foo`` always names an
external framework/package. This file asserts that contract directly (every
extracted import stays bare/external through the confidence resolver), rather than
letting a silent 0% EXTRACTED rate be mistaken for a resolver gap.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import Mock

import pytest

from brain.memory import IMPORT_EXTRACTORS, _extract_swift_imports, _resolve_edge_confidence
from core.ast_engine import ASTEngine
from shared.contracts import IndexingRequest


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str) -> IndexingRequest:
    return IndexingRequest(file_path=path, content="", language_id="swift", workspace_root="/ws")


def test_swift_extraction_succeeds() -> None:
    src = "import Foundation\nimport UIKit\n"
    tree = _parse(src, "swift", "/ws/Main.swift")
    imports = _extract_swift_imports(tree, _req("/ws/Main.swift"))
    assert imports == ["Foundation", "UIKit"]


def test_swift_import_never_resolves_even_against_a_same_named_local_file() -> None:
    """The expected/documented behaviour: even if a project happens to have a
    sibling ``Foundation.swift``-shaped file, Swift's ``import`` statement was
    never designed to reference it — it always names an external module, so it
    must stay INFERRED, never EXTRACTED, regardless of what's indexed."""
    edges = (("/ws/Main.swift", "Foundation"),)
    indexed = ("/ws/Main.swift", "/ws/Foundation.swift")
    result = _resolve_edge_confidence(edges, indexed)
    assert result == (("/ws/Main.swift", "Foundation", "INFERRED", 0.5),)


def test_swift_registered() -> None:
    assert IMPORT_EXTRACTORS.get("swift") is not None


def test_swift_extraction_is_filesystem_free(monkeypatch: pytest.MonkeyPatch) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse("import Foundation\n", "swift", "/ws/Main.swift")
    _extract_swift_imports(tree, _req("/ws/Main.swift"))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()
