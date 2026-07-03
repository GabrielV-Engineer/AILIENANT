"""Polyglot import extraction — new-dependency languages: PHP, Dart.

Isolated from the no-new-dependency families (``tree-sitter-php``,
``tree-sitter-dart`` are new pinned packages, a different risk category — see
``docs/TECH_DEBT_BACKLOG.md`` for the ``tree-sitter-dart`` single-release-package
note). Mirrors the established convention: real-grammar parse via
``ASTEngine().parse``, exact ordered import-list assertions, workspace-escape drop,
filesystem-free assertion, registry dispatch — plus PHP's four require/include
keyword variants + namespace-``use`` resolution, and Dart's three distinct
resolution shapes (``dart:``, ``package:``, relative).
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import Mock

import pytest

from brain.memory import IMPORT_EXTRACTORS, _extract_dart_imports, _extract_php_imports
from core.ast_engine import ASTEngine
from core.module_resolver import FAMILY_TABLE
from shared.contracts import IndexingRequest

_BS = chr(92)  # constructed defensively, not a literal escape in source — see the
# PowerShell test file's note on why.


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str, language_id: str) -> IndexingRequest:
    return IndexingRequest(file_path=path, content="", language_id=language_id, workspace_root="/ws")


# ── PHP ───────────────────────────────────────────────────────────────────


def test_php_require_variants_all_resolved() -> None:
    # PHP code must live inside a `<?php ?>` tag to be parsed as PHP at all — the
    # grammar treats anything outside it as opaque HTML/text.
    src = (
        "<?php\n"
        'require "foo.php";\n'
        'require_once "./bar.php";\n'
        'include "baz.php";\n'
        'include_once("qux.php");\n'
    )
    tree = _parse(src, "php", "/ws/main.php")
    imports = _extract_php_imports(tree, _req("/ws/main.php", "php"))
    assert imports == ["/ws/foo.php", "/ws/bar.php", "/ws/baz.php", "/ws/qux.php"]


def test_php_namespace_use_target() -> None:
    src = "<?php\nuse " + "Foo" + _BS + "Bar;\n"
    tree = _parse(src, "php", "/ws/main.php")
    imports = _extract_php_imports(tree, _req("/ws/main.php", "php"))
    assert imports == ["Foo" + _BS + "Bar"]


def test_php_workspace_escape_dropped() -> None:
    src = '<?php\nrequire "../../../etc/passwd";\n'
    tree = _parse(src, "php", "/ws/main.php")
    imports = _extract_php_imports(tree, _req("/ws/main.php", "php"))
    assert imports == []


def test_php_family_registered_with_backslash_separator() -> None:
    assert FAMILY_TABLE["php"].separator == _BS
    assert FAMILY_TABLE["php"].extensions == (".php",)


# ── Dart ──────────────────────────────────────────────────────────────────


def test_dart_builtin_import_stays_bare() -> None:
    src = "import 'dart:core';\n"
    tree = _parse(src, "dart", "/ws/main.dart")
    imports = _extract_dart_imports(tree, _req("/ws/main.dart", "dart"))
    assert imports == ["dart:core"]


def test_dart_package_uri_strips_prefix() -> None:
    src = "import 'package:foo/bar.dart';\n"
    tree = _parse(src, "dart", "/ws/main.dart")
    imports = _extract_dart_imports(tree, _req("/ws/main.dart", "dart"))
    assert imports == ["foo/bar.dart"]


def test_dart_relative_import_resolved() -> None:
    src = "import 'sibling.dart';\n"
    tree = _parse(src, "dart", "/ws/src/main.dart")
    imports = _extract_dart_imports(tree, _req("/ws/src/main.dart", "dart"))
    assert imports == ["/ws/src/sibling.dart"]


def test_dart_aliased_import_still_extracts_the_uri() -> None:
    src = "import 'package:foo/bar.dart' as fb;\n"
    tree = _parse(src, "dart", "/ws/main.dart")
    imports = _extract_dart_imports(tree, _req("/ws/main.dart", "dart"))
    assert imports == ["foo/bar.dart"]


def test_dart_workspace_escape_dropped() -> None:
    src = "import '../../../etc/passwd.dart';\n"
    tree = _parse(src, "dart", "/ws/main.dart")
    imports = _extract_dart_imports(tree, _req("/ws/main.dart", "dart"))
    assert imports == []


# ── Filesystem-free + registry dispatch ──────────────────────────────────────


def test_new_deps_extraction_is_filesystem_free(monkeypatch: pytest.MonkeyPatch) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse('<?php\nrequire_once "./bar.php";\n', "php", "/ws/main.php")
    _extract_php_imports(tree, _req("/ws/main.php", "php"))
    tree = _parse("import 'sibling.dart';\n", "dart", "/ws/main.dart")
    _extract_dart_imports(tree, _req("/ws/main.dart", "dart"))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


def test_new_deps_languages_registered() -> None:
    for lang in ("php", "dart"):
        assert IMPORT_EXTRACTORS.get(lang) is not None, f"{lang} missing from IMPORT_EXTRACTORS"
