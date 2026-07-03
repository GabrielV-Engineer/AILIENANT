"""Polyglot import extraction — scripting languages: Ruby, Lua, Bash, PowerShell.

Mirrors ``test_polyglot_imports.py``'s convention plus family-specific cases:
Ruby's ``require`` (bare/external) vs ``require_relative`` (resolved) split, Lua's
dot-separated ``require()`` convention, Bash's exact ``source``/``.`` command-name
match, PowerShell's case-insensitive command matching and its two distinct
node shapes (dot-sourcing vs ``Import-Module``).
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import Mock

import pytest

from brain.memory import (
    IMPORT_EXTRACTORS,
    _extract_bash_imports,
    _extract_lua_imports,
    _extract_powershell_imports,
    _extract_ruby_imports,
)
from core.ast_engine import ASTEngine
from shared.contracts import IndexingRequest

_BS = chr(92)  # a literal backslash, constructed defensively — PowerShell's native
# separator is backslash, and building it via an escape sequence inside a Python
# string source risks a misread escape (`\f` etc.) depending on how the literal
# reaches the interpreter.


def _parse(content: str, language_id: str, path: str) -> Any:
    tree = ASTEngine().parse(path, content, language_id)
    assert tree is not None, f"grammar for {language_id} failed to parse"
    return tree


def _req(path: str) -> IndexingRequest:
    return IndexingRequest(file_path=path, content="", language_id="ruby", workspace_root="/ws")


# ── Ruby ──────────────────────────────────────────────────────────────────


def test_ruby_require_is_bare_external() -> None:
    src = 'require "foo"\n'
    tree = _parse(src, "ruby", "/ws/main.rb")
    imports = _extract_ruby_imports(tree, _req("/ws/main.rb"))
    assert imports == ["foo"]


def test_ruby_require_relative_is_resolved() -> None:
    src = 'require_relative "./bar"\n'
    tree = _parse(src, "ruby", "/ws/main.rb")
    imports = _extract_ruby_imports(tree, _req("/ws/main.rb"))
    assert imports == ["/ws/bar"]


def test_ruby_workspace_escape_dropped() -> None:
    src = 'require_relative "../../../etc/passwd"\n'
    tree = _parse(src, "ruby", "/ws/main.rb")
    imports = _extract_ruby_imports(tree, _req("/ws/main.rb"))
    assert imports == []


# ── Lua ───────────────────────────────────────────────────────────────────


def test_lua_require_dotted_specifier() -> None:
    src = 'local m = require("mymodule.sub")\n'
    tree = _parse(src, "lua", "/ws/main.lua")
    imports = _extract_lua_imports(tree, _req("/ws/main.lua"))
    assert imports == ["mymodule.sub"]


def test_lua_bare_require() -> None:
    src = 'require("mymod")\n'
    tree = _parse(src, "lua", "/ws/main.lua")
    imports = _extract_lua_imports(tree, _req("/ws/main.lua"))
    assert imports == ["mymod"]


# ── Bash ──────────────────────────────────────────────────────────────────


def test_bash_source_command() -> None:
    src = "source ./foo.sh\n"
    tree = _parse(src, "shellscript", "/ws/main.sh")
    imports = _extract_bash_imports(tree, _req("/ws/main.sh"))
    assert imports == ["/ws/foo.sh"]


def test_bash_dot_command_alias() -> None:
    src = ". ./bar.sh\n"
    tree = _parse(src, "shellscript", "/ws/main.sh")
    imports = _extract_bash_imports(tree, _req("/ws/main.sh"))
    assert imports == ["/ws/bar.sh"]


def test_bash_quoted_source_argument() -> None:
    src = 'source "lib/baz.sh"\n'
    tree = _parse(src, "shellscript", "/ws/main.sh")
    imports = _extract_bash_imports(tree, _req("/ws/main.sh"))
    assert imports == ["/ws/lib/baz.sh"]


def test_bash_unrelated_command_not_matched() -> None:
    src = "echo hello\n"
    tree = _parse(src, "shellscript", "/ws/main.sh")
    imports = _extract_bash_imports(tree, _req("/ws/main.sh"))
    assert imports == []


# ── PowerShell ────────────────────────────────────────────────────────────


def test_powershell_dot_sourcing_resolved() -> None:
    src = ". ." + _BS + "foo.ps1\n"
    tree = _parse(src, "powershell", "/ws/main.ps1")
    imports = _extract_powershell_imports(tree, _req("/ws/main.ps1"))
    assert imports == ["/ws/foo.ps1"]


def test_powershell_import_module_relative_path_resolved() -> None:
    src = "Import-Module ." + _BS + "bar.psm1\n"
    tree = _parse(src, "powershell", "/ws/main.ps1")
    imports = _extract_powershell_imports(tree, _req("/ws/main.ps1"))
    assert imports == ["/ws/bar.psm1"]


def test_powershell_import_module_bare_name_is_external() -> None:
    src = "Import-Module MyModule\n"
    tree = _parse(src, "powershell", "/ws/main.ps1")
    imports = _extract_powershell_imports(tree, _req("/ws/main.ps1"))
    assert imports == ["MyModule"]


def test_powershell_command_matching_is_case_insensitive() -> None:
    src = "import-module MyModule\n"
    tree = _parse(src, "powershell", "/ws/main.ps1")
    imports = _extract_powershell_imports(tree, _req("/ws/main.ps1"))
    assert imports == ["MyModule"]


# ── Filesystem-free + registry dispatch ──────────────────────────────────────


def test_scripting_extraction_is_filesystem_free(monkeypatch: pytest.MonkeyPatch) -> None:
    exists_spy = Mock(side_effect=AssertionError("os.path.exists must not be called"))
    isfile_spy = Mock(side_effect=AssertionError("os.path.isfile must not be called"))
    monkeypatch.setattr(os.path, "exists", exists_spy)
    monkeypatch.setattr(os.path, "isfile", isfile_spy)

    tree = _parse('require_relative "./bar"\n', "ruby", "/ws/main.rb")
    _extract_ruby_imports(tree, _req("/ws/main.rb"))
    tree = _parse("source ./foo.sh\n", "shellscript", "/ws/main.sh")
    _extract_bash_imports(tree, _req("/ws/main.sh"))
    exists_spy.assert_not_called()
    isfile_spy.assert_not_called()


def test_scripting_languages_registered() -> None:
    for lang in ("ruby", "lua", "shellscript", "powershell"):
        assert IMPORT_EXTRACTORS.get(lang) is not None, f"{lang} missing from IMPORT_EXTRACTORS"
