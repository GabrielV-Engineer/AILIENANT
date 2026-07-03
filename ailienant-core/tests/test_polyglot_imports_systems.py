"""Polyglot import extraction — systems languages: C, C++, Rust, Go, Zig.

Mirrors ``test_polyglot_imports.py``'s convention: real-grammar parse via
``ASTEngine().parse``, exact ordered import-list assertions, workspace-escape drop,
filesystem-free assertion, registry dispatch — plus family-specific cases: Rust's
grouped/aliased ``use`` expansion and its ``mod``-vs-inline-module distinction,
Go's grouped/aliased ``import (...)`` block, and Zig's relative-vs-bare ``@import``.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import Mock

import pytest

from brain.memory import (
    IMPORT_EXTRACTORS,
    _extract_c_family_includes,
    _extract_go_imports,
    _extract_rust_imports,
    _extract_zig_imports,
)
from core.ast_engine import ASTEngine
from shared.contracts import IndexingRequest


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str, workspace_root: str = "/ws") -> IndexingRequest:
    return IndexingRequest(file_path=path, content="", language_id="c", workspace_root=workspace_root)


# ── C / C++ ────────────────────────────────────────────────────────────────


def test_c_angle_bracket_is_bare_quoted_is_relative() -> None:
    src = '#include <stdio.h>\n#include "local.h"\n'
    tree = _parse(src, "c", "/ws/src/main.c")
    imports = _extract_c_family_includes(tree, _req("/ws/src/main.c"))
    assert imports == ["stdio.h", "/ws/src/local.h"]


def test_cpp_shares_the_same_extractor() -> None:
    src = '#include <vector>\n#include "local.hpp"\n'
    tree = _parse(src, "cpp", "/ws/src/main.cpp")
    imports = _extract_c_family_includes(tree, _req("/ws/src/main.cpp"))
    assert imports == ["vector", "/ws/src/local.hpp"]


def test_c_quoted_include_keeps_its_extension() -> None:
    # Unlike JS's extensionless specifiers, C/C++ paths already carry a real
    # extension — no candidate-expansion guessing should be needed downstream.
    src = '#include "sub/local.h"\n'
    tree = _parse(src, "c", "/ws/src/main.c")
    imports = _extract_c_family_includes(tree, _req("/ws/src/main.c"))
    assert imports == ["/ws/src/sub/local.h"]


def test_c_workspace_escape_dropped() -> None:
    src = '#include "../../../etc/passwd"\n'
    tree = _parse(src, "c", "/ws/src/main.c")
    imports = _extract_c_family_includes(tree, _req("/ws/src/main.c"))
    assert imports == []


# ── Rust ──────────────────────────────────────────────────────────────────


def test_rust_scoped_use_full_text() -> None:
    src = "use crate::foo::Bar;\nuse std::collections::HashMap;\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == ["crate::foo::Bar", "std::collections::HashMap"]


def test_rust_grouped_use_expands_into_multiple_targets() -> None:
    src = "use std::{fmt, io::Read};\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == ["std::fmt", "std::io::Read"]


def test_rust_use_as_clause_ignores_the_alias() -> None:
    src = "use foo::Bar as Baz;\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == ["foo::Bar"]


def test_rust_bodyless_mod_maps_to_sibling_bare_name() -> None:
    src = "mod baz;\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == ["baz"]


def test_rust_inline_mod_with_body_names_no_file() -> None:
    src = "mod inline { fn x() {} }\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == []


def test_rust_bare_external_crate_emitted_verbatim() -> None:
    src = "use serde::Deserialize;\n"
    tree = _parse(src, "rust", "/ws/src/lib.rs")
    imports = _extract_rust_imports(tree, _req("/ws/src/lib.rs"))
    assert imports == ["serde::Deserialize"]


# ── Go ────────────────────────────────────────────────────────────────────


def test_go_single_import() -> None:
    src = 'package main\nimport "fmt"\n'
    tree = _parse(src, "go", "/ws/main.go")
    imports = _extract_go_imports(tree, _req("/ws/main.go"))
    assert imports == ["fmt"]


def test_go_grouped_import_with_alias_ignores_alias() -> None:
    src = 'package main\nimport (\n  f "fmt"\n  "github.com/pkg/errors"\n)\n'
    tree = _parse(src, "go", "/ws/main.go")
    imports = _extract_go_imports(tree, _req("/ws/main.go"))
    assert imports == ["fmt", "github.com/pkg/errors"]


def test_go_directory_granularity_resolves_to_every_file_in_package() -> None:
    from core.module_resolver import build_all_family_indices, resolve_via_suffix_index

    indexed = ("/ws/pkg/util/a.go", "/ws/pkg/util/b.go")
    idx = build_all_family_indices(indexed)
    hits = resolve_via_suffix_index("pkg/util", "/", idx["go"])
    assert sorted(hits) == ["/ws/pkg/util/a.go", "/ws/pkg/util/b.go"]


# ── Zig ───────────────────────────────────────────────────────────────────


def test_zig_bare_import_is_external() -> None:
    src = 'const std = @import("std");\n'
    tree = _parse(src, "zig", "/ws/main.zig")
    imports = _extract_zig_imports(tree, _req("/ws/main.zig"))
    assert imports == ["std"]


def test_zig_relative_import_is_resolved() -> None:
    src = 'const foo = @import("./foo.zig");\n'
    tree = _parse(src, "zig", "/ws/main.zig")
    imports = _extract_zig_imports(tree, _req("/ws/main.zig"))
    assert imports == ["/ws/foo.zig"]


def test_zig_workspace_escape_dropped() -> None:
    src = 'const x = @import("../../../etc/passwd");\n'
    tree = _parse(src, "zig", "/ws/main.zig")
    imports = _extract_zig_imports(tree, _req("/ws/main.zig"))
    assert imports == []


# ── Filesystem-free + registry dispatch ──────────────────────────────────────


def test_systems_extraction_is_filesystem_free(monkeypatch: pytest.MonkeyPatch) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse('#include "local.h"\n', "c", "/ws/main.c")
    _extract_c_family_includes(tree, _req("/ws/main.c"))
    tree = _parse("use crate::foo::Bar;\n", "rust", "/ws/lib.rs")
    _extract_rust_imports(tree, _req("/ws/lib.rs"))
    tree = _parse('import "fmt"\n', "go", "/ws/main.go")
    _extract_go_imports(tree, _req("/ws/main.go"))
    tree = _parse('const x = @import("std");\n', "zig", "/ws/main.zig")
    _extract_zig_imports(tree, _req("/ws/main.zig"))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


def test_systems_languages_registered() -> None:
    for lang in ("c", "cpp", "rust", "go", "zig"):
        assert IMPORT_EXTRACTORS.get(lang) is not None, f"{lang} missing from IMPORT_EXTRACTORS"
