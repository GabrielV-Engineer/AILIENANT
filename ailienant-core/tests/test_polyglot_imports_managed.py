"""Polyglot import extraction — managed/enterprise languages: Java, Kotlin, C#,
Scala, Elixir, Haskell.

Mirrors ``test_polyglot_imports.py``'s convention plus family-specific cases: Java/
Kotlin wildcard+alias handling, Scala's selector-group/rename expansion, C#'s
verified alias field-name trap, Elixir's grouped-alias expansion, Haskell's
qualified/``as``-alias disambiguation by field name.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import Mock

import pytest

from brain.memory import (
    IMPORT_EXTRACTORS,
    _extract_csharp_imports,
    _extract_elixir_imports,
    _extract_haskell_imports,
    _extract_java_imports,
    _extract_kotlin_imports,
    _extract_scala_imports,
)
from core.ast_engine import ASTEngine
from shared.contracts import IndexingRequest


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str) -> IndexingRequest:
    return IndexingRequest(file_path=path, content="", language_id="java", workspace_root="/ws")


# ── Java ──────────────────────────────────────────────────────────────────


def test_java_simple_import() -> None:
    src = "import com.foo.Bar;\n"
    tree = _parse(src, "java", "/ws/Main.java")
    imports = _extract_java_imports(tree, _req("/ws/Main.java"))
    assert imports == ["com.foo.Bar"]


def test_java_wildcard_import_is_package_prefix_only() -> None:
    src = "import com.foo.*;\n"
    tree = _parse(src, "java", "/ws/Main.java")
    imports = _extract_java_imports(tree, _req("/ws/Main.java"))
    assert imports == ["com.foo"]


def test_java_static_import_resolves_like_any_other() -> None:
    src = "import static java.util.Collections.emptyList;\n"
    tree = _parse(src, "java", "/ws/Main.java")
    imports = _extract_java_imports(tree, _req("/ws/Main.java"))
    assert imports == ["java.util.Collections.emptyList"]


# ── Kotlin ────────────────────────────────────────────────────────────────


def test_kotlin_simple_import() -> None:
    src = "import com.foo.Bar\n"
    tree = _parse(src, "kotlin", "/ws/Main.kt")
    imports = _extract_kotlin_imports(tree, _req("/ws/Main.kt"))
    assert imports == ["com.foo.Bar"]


def test_kotlin_wildcard_and_alias_forms() -> None:
    src = "import com.foo.*\nimport com.bar.Baz as B\n"
    tree = _parse(src, "kotlin", "/ws/Main.kt")
    imports = _extract_kotlin_imports(tree, _req("/ws/Main.kt"))
    assert imports == ["com.foo", "com.bar.Baz"]


# ── Scala ─────────────────────────────────────────────────────────────────


def test_scala_simple_import() -> None:
    src = "import com.foo.bar.Baz\n"
    tree = _parse(src, "scala", "/ws/Main.scala")
    imports = _extract_scala_imports(tree, _req("/ws/Main.scala"))
    assert imports == ["com.foo.bar.Baz"]


def test_scala_wildcard_import() -> None:
    src = "import com.foo._\n"
    tree = _parse(src, "scala", "/ws/Main.scala")
    imports = _extract_scala_imports(tree, _req("/ws/Main.scala"))
    assert imports == ["com.foo"]


def test_scala_selector_group_expands_and_rename_uses_original_name() -> None:
    src = "import com.foo.{A => Renamed, B}\n"
    tree = _parse(src, "scala", "/ws/Main.scala")
    imports = _extract_scala_imports(tree, _req("/ws/Main.scala"))
    # The rename target resolves by its ORIGINAL name ("A"), never the local
    # alias ("Renamed") — a rename has no bearing on which file it names.
    assert imports == ["com.foo.A", "com.foo.B"]


# ── C# ────────────────────────────────────────────────────────────────────


def test_csharp_plain_using() -> None:
    src = "using System;\nusing Foo.Bar;\n"
    tree = _parse(src, "csharp", "/ws/Main.cs")
    imports = _extract_csharp_imports(tree, _req("/ws/Main.cs"))
    assert imports == ["System", "Foo.Bar"]


def test_csharp_aliased_using_extracts_target_not_alias() -> None:
    """Regression test for the verified field-name trap: ``child_by_field_name
    ("name")`` on an aliased ``using`` returns the ALIAS, not the target."""
    src = "using Alias = MyNamespace.Other;\n"
    tree = _parse(src, "csharp", "/ws/Main.cs")
    imports = _extract_csharp_imports(tree, _req("/ws/Main.cs"))
    assert imports == ["MyNamespace.Other"]
    assert "Alias" not in imports


# ── Elixir ────────────────────────────────────────────────────────────────


def test_elixir_single_alias() -> None:
    src = "defmodule Foo do\n  alias MyApp.Bar\nend\n"
    tree = _parse(src, "elixir", "/ws/foo.ex")
    imports = _extract_elixir_imports(tree, _req("/ws/foo.ex"))
    assert imports == ["MyApp.Bar"]


def test_elixir_grouped_alias_expands_into_multiple_targets() -> None:
    src = "defmodule Foo do\n  alias MyApp.{Bar, Baz}\nend\n"
    tree = _parse(src, "elixir", "/ws/foo.ex")
    imports = _extract_elixir_imports(tree, _req("/ws/foo.ex"))
    assert imports == ["MyApp.Bar", "MyApp.Baz"]


def test_elixir_import_require_use_all_recognized() -> None:
    src = "defmodule Foo do\n  import MyApp.A\n  require MyApp.B\n  use MyApp.C\nend\n"
    tree = _parse(src, "elixir", "/ws/foo.ex")
    imports = _extract_elixir_imports(tree, _req("/ws/foo.ex"))
    assert imports == ["MyApp.A", "MyApp.B", "MyApp.C"]


# ── Haskell ───────────────────────────────────────────────────────────────


def test_haskell_simple_import() -> None:
    src = "module Main where\nimport Data.List\n"
    tree = _parse(src, "haskell", "/ws/Main.hs")
    imports = _extract_haskell_imports(tree, _req("/ws/Main.hs"))
    assert imports == ["Data.List"]


def test_haskell_qualified_as_extracts_real_target_not_alias() -> None:
    """The grammar disambiguates by field name (``module`` vs ``alias``) — no
    positional guessing needed, unlike C#."""
    src = "module Main where\nimport qualified Data.Map as Map\n"
    tree = _parse(src, "haskell", "/ws/Main.hs")
    imports = _extract_haskell_imports(tree, _req("/ws/Main.hs"))
    assert imports == ["Data.Map"]
    assert "Map" not in imports


def test_haskell_selective_import_list_does_not_leak_as_a_module() -> None:
    src = "module Main where\nimport Data.Map (fromList)\n"
    tree = _parse(src, "haskell", "/ws/Main.hs")
    imports = _extract_haskell_imports(tree, _req("/ws/Main.hs"))
    assert imports == ["Data.Map"]


# ── Filesystem-free + registry dispatch ──────────────────────────────────────


def test_managed_extraction_is_filesystem_free(monkeypatch: pytest.MonkeyPatch) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse("import com.foo.Bar;\n", "java", "/ws/Main.java")
    _extract_java_imports(tree, _req("/ws/Main.java"))
    tree = _parse("using Foo.Bar;\n", "csharp", "/ws/Main.cs")
    _extract_csharp_imports(tree, _req("/ws/Main.cs"))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


def test_managed_languages_registered() -> None:
    for lang in ("java", "kotlin", "scala", "csharp", "elixir", "haskell"):
        assert IMPORT_EXTRACTORS.get(lang) is not None, f"{lang} missing from IMPORT_EXTRACTORS"
