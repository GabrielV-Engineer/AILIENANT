"""
brain/memory.py — Process-pool-safe indexing bridge.

All functions are module-level so ProcessPoolExecutor can pickle them.
Phase 3 extends this module with LanceDB vector indexing and GraphRAG topology extraction.
"""
from __future__ import annotations

import logging
import os
import posixpath
from collections import Counter
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core.module_resolver import FAMILY_TABLE, build_all_family_indices, family_for_source, resolve_via_suffix_index
from shared.contracts import IndexingRequest, IndexingResult, PPRRequest, PPRResult, SymbolDef

logger = logging.getLogger("MEMORY_WORKER")

# Upper bound on the dependency-graph edge count a single PPR / analytics call
# will build. networkx is pure-Python (dict-of-dict-of-dict) with large per-node
# and per-edge heap overhead, and the undirected projection briefly doubles the
# structure — refusing oversized graphs caps the transient heap spike per call so
# a pathologically large workspace cannot stall the pooled worker. Gating on the
# edge count keeps the check O(1) and pre-build (the node count, on the order of
# the edge count for a sparse dependency graph, is only known after building).
MAX_GRAPH_EDGES: int = 5000

# Per-process singleton — initialized once by _worker_init(), never shared across processes.
_worker_ast: Optional[Any] = None


def _worker_init() -> None:
    """Called once per worker process by ProcessPoolExecutor(initializer=_worker_init)."""
    global _worker_ast
    from core.ast_engine import ASTEngine
    _worker_ast = ASTEngine()


def _count_top_level_symbols(tree: Any) -> int:
    if tree is None:
        return 0
    return sum(1 for node in tree.root_node.children if node.is_named)


def _extract_python_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Walk root_node children for Python import_statement and import_from nodes.

    Returns absolute module paths only (e.g. 'brain.state', 'shared.config').
    ``req`` is accepted for registry-uniform dispatch and unused here — Python
    imports are already absolute module paths and need no lexical resolution.
    """
    imports: list[str] = []
    for node in tree.root_node.children:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    text = child.text.decode("utf-8")
                    if text:
                        imports.append(text)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        text = name_node.text.decode("utf-8")
                        if text:
                            imports.append(text)
        elif node.type in ("import_from_statement", "import_from"):
            module_node = node.child_by_field_name("module_name")
            if module_node is None:
                continue
            text = module_node.text.decode("utf-8")
            # TODO(DEBT-087): relative imports ("from .mod import x") are skipped —
            # TS/JS now resolve relatives lexically, so Python module boundaries are
            # asymmetric in the dependency graph until resolution is added here too.
            if text and not text.startswith("."):
                imports.append(text)
    return imports


# JavaScript/TypeScript source extensions, tried when resolving an extensionless
# relative specifier and stripped from a specifier that carries one explicitly.
_JS_TS_EXTS: Tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _string_literal_text(string_node: Any) -> str:
    """Return the unquoted content of a tree-sitter ``string``-shaped node.

    Prefers the grammar-provided, quote-free content child — named ``string_fragment``
    in the ECMAScript grammars, ``string_content`` in C/C++/Ruby/Lua/Zig/Bash's — and
    falls back to stripping the surrounding quote characters from the raw node text.
    """
    for child in string_node.children:
        if child.type in ("string_fragment", "string_content"):
            return child.text.decode("utf-8")
    raw = string_node.text.decode("utf-8")
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return ""


def _resolve_relative_specifier(
    spec: str, req: IndexingRequest, strip_exts: Tuple[str, ...] = _JS_TS_EXTS
) -> Optional[str]:
    """Lexically resolve a relative specifier to a workspace path.

    Pure string math — no filesystem access. Uses ``posixpath`` on forward-slashed
    input so a Windows-origin path (``C:\\ws\\a.ts``) resolves identically on a Linux
    or Alpine worker (where ``os.path`` treats ``\\`` as an ordinary filename char).
    A specifier that escapes ``workspace_root`` is dropped (returns ``None``).

    ``strip_exts`` controls whether a trailing recognized extension is stripped from
    the result: TS/JS specifiers are conventionally extensionless (the default,
    ``_JS_TS_EXTS``), but C/C++/PowerShell/Ruby-relative/Zig specifiers already carry
    their real extension — callers for those pass ``strip_exts=()`` so the resolved
    path keeps it.
    """
    base = posixpath.dirname(req.file_path.replace("\\", "/"))
    normalized = posixpath.normpath(posixpath.join(base, spec))
    for ext in strip_exts:
        if normalized.endswith(ext):
            normalized = normalized[: -len(ext)]
            break
    ws = req.workspace_root.replace("\\", "/")
    if ws:
        safe_root = ws.rstrip("/") + "/"
        if not (normalized + "/").startswith(safe_root):
            return None  # directory escape — drop edge
    return normalized


def _extract_ecmascript_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract module dependencies from a TypeScript/JavaScript AST.

    One walk serves all TS/JS variants (the grammars emit identical import node
    types). Captures static ``import``/re-export ``export … from``, dynamic
    ``import('…')``, and ``require('…')`` — the latter two nest arbitrarily, so
    the whole tree is walked, not just top-level nodes. Bare/package specifiers
    are emitted as-is (resolved to INFERRED downstream); relative specifiers are
    lexically resolved and workspace-confined. Template/computed specifiers are
    non-lexical and skipped. Order-preserving dedup keeps the edge list clean.
    """
    specs: List[str] = []
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None:
                text = _string_literal_text(source)
                if text:
                    specs.append(text)
        elif node_type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and (
                func.type == "import"
                or (func.type == "identifier" and func.text == b"require")
            ):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for child in args.children:
                        if child.type == "string":
                            text = _string_literal_text(child)
                            if text:
                                specs.append(text)
                            break
        # Push children reversed so the stack yields them in document (pre-order)
        # order — dependency edges preserve the source's import ordering.
        stack.extend(reversed(node.children))

    out: List[str] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.startswith("."):
            resolved = _resolve_relative_specifier(spec, req)
            if resolved is None:
                continue
            target = resolved
        else:
            target = spec
        if target not in seen:
            seen.add(target)
            out.append(target)
    return out


def _extract_c_family_includes(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``#include`` targets from a C/C++ AST.

    Angle-bracket includes (``<stdio.h>``) are system headers — always bare/
    external. Quoted includes (``"local.h"``) already carry their real extension
    (no guessing needed, unlike JS's extensionless specifiers) and are lexically
    resolved via the same relative-specifier math with extension-stripping
    disabled.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "preproc_include":
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                if path_node.type == "system_lib_string":
                    text = path_node.text.decode("utf-8").strip("<>")
                    if text and text not in seen:
                        seen.add(text)
                        out.append(text)
                elif path_node.type == "string_literal":
                    text = _string_literal_text(path_node)
                    if text:
                        resolved = _resolve_relative_specifier(text, req, strip_exts=())
                        if resolved is not None and resolved not in seen:
                            seen.add(resolved)
                            out.append(resolved)
        stack.extend(reversed(node.children))
    return out


def _extract_rust_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``use``- and ``mod``-declaration targets from a Rust AST.

    A ``scoped_identifier``'s full text already concatenates its ``::``-chain (no
    field-by-field path/name walk needed). A grouped ``use a::{b, c::d};`` expands
    into N separate targets, each reconstructed as ``<prefix>::<member>``. A
    body-less ``mod foo;`` maps to a sibling file/subdirectory — the SAME
    suffix-index resolver that handles ``use`` naturally matches a bare trailing
    segment, so no separate resolution path is needed. A bare external-crate root
    (``serde``, ``std`` — not ``crate``/``self``/``super``-prefixed) is emitted the
    same as everything else; it simply never matches the suffix index and falls
    through to INFERRED, mirroring every other family's bare/external handling.
    """
    out: List[str] = []
    seen: set[str] = set()

    def _emit(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    def _expand_list(prefix: str, list_node: Any) -> None:
        for child in list_node.children:
            if child.type in ("identifier", "scoped_identifier"):
                _emit(f"{prefix}::{child.text.decode('utf-8')}")

    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg is not None:
                if arg.type == "use_as_clause":
                    path = arg.child_by_field_name("path")
                    if path is not None:
                        _emit(path.text.decode("utf-8"))
                elif arg.type == "scoped_use_list":
                    path = arg.child_by_field_name("path")
                    lst = arg.child_by_field_name("list")
                    prefix = path.text.decode("utf-8") if path is not None else ""
                    if lst is not None:
                        _expand_list(prefix, lst)
                else:
                    _emit(arg.text.decode("utf-8"))
        elif node.type == "mod_item":
            name = node.child_by_field_name("name")
            # A body-less `mod foo;` always ends in a bare `;`; `mod foo { ... }` is
            # inline and names no external file.
            if name is not None and node.children and node.children[-1].type == ";":
                _emit(name.text.decode("utf-8"))
        stack.extend(reversed(node.children))
    return out


def _extract_go_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Go AST.

    Handles both the single ``import "fmt"`` and grouped ``import (...)`` forms; an
    aliased entry (``import f "fmt"``) still yields the real path, ignoring the
    alias. Targets are package paths (``github.com/pkg/errors``), resolved at
    directory granularity — a target names every ``.go`` file in the matching
    directory, not one file (see ``core.module_resolver``).
    """
    out: List[str] = []
    seen: set[str] = set()

    def _spec_text(spec: Any) -> Optional[str]:
        path = spec.child_by_field_name("path")
        if path is None or path.type != "interpreted_string_literal":
            return None
        for child in path.children:
            if child.type == "interpreted_string_literal_content":
                return child.text.decode("utf-8")
        return None

    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    text = _spec_text(child)
                    if text and text not in seen:
                        seen.add(text)
                        out.append(text)
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            text = _spec_text(spec)
                            if text and text not in seen:
                                seen.add(text)
                                out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_java_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Java AST.

    ``scoped_identifier``'s full text already concatenates the dotted chain (no
    field-by-field walk needed). A trailing ``asterisk`` sibling marks a wildcard
    import — since it sits AFTER the ``scoped_identifier``, the extracted text is
    already the package prefix alone. ``static`` member imports resolve
    identically (the containing class is what matters for file resolution). Java
    enforces one-public-class-per-file matching the filename — expect the
    highest resolution reliability of any dotted-family language here.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            scoped = next((c for c in node.children if c.type == "scoped_identifier"), None)
            if scoped is not None:
                text = scoped.text.decode("utf-8")
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_kotlin_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Kotlin AST.

    Unlike Java's recursively-nested ``scoped_identifier``, Kotlin's
    ``qualified_identifier`` is a FLAT sibling sequence of ``identifier``/``.``
    tokens — its own ``.text`` already excludes a trailing wildcard ``*`` or
    ``as``-alias clause (both are siblings after it, not part of it). Kotlin does
    not enforce file-per-class — expect lower recall than Java.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import":
            qid = next((c for c in node.children if c.type == "qualified_identifier"), None)
            if qid is not None:
                text = qid.text.decode("utf-8")
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_scala_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Scala AST.

    A flat ``identifier``/``.`` sequence forms the base path. A trailing
    ``namespace_wildcard`` (``_``) is a wildcard — the collected prefix alone is
    emitted. A trailing ``namespace_selectors`` (``{A, B}`` / ``{A => Renamed}``)
    expands into N targets — a rename selector (``arrow_renamed_identifier``)
    resolves by its ORIGINAL name (field ``name``), never the local alias (field
    ``alias``).
    """
    out: List[str] = []
    seen: set[str] = set()

    def _emit(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            prefix_parts: List[str] = []
            selectors: Optional[Any] = None
            for child in node.children:
                if child.type == "identifier":
                    prefix_parts.append(child.text.decode("utf-8"))
                elif child.type == "namespace_selectors":
                    selectors = child
                # "namespace_wildcard" and "." tokens contribute nothing further.
            prefix = ".".join(prefix_parts)
            if selectors is not None:
                for sel in selectors.children:
                    if sel.type == "identifier":
                        _emit(f"{prefix}.{sel.text.decode('utf-8')}")
                    elif sel.type == "arrow_renamed_identifier":
                        name = sel.child_by_field_name("name")
                        if name is not None:
                            _emit(f"{prefix}.{name.text.decode('utf-8')}")
            elif prefix:
                _emit(prefix)
        stack.extend(reversed(node.children))
    return out


def _extract_csharp_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract using-directive targets from a C# AST.

    VERIFIED TRAP: for an aliased ``using Alias = Foo.Bar;``,
    ``child_by_field_name("name")`` returns the ALIAS identifier ("Alias"), not
    the target — the real target (``qualified_name``/``identifier``) carries no
    field name at all in that form. Extraction is therefore POSITIONAL: always
    take the last non-punctuation child, never trust the ``name`` field. C#
    namespaces are not required to mirror the directory layout (unlike Java's
    compiler-enforced convention) — expect lower resolution recall than Java/
    Python/Rust for this family.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "using_directive":
            candidates = [
                c for c in node.children if c.type not in ("using", ";", "static", "=")
            ]
            target_node = candidates[-1] if candidates else None
            if target_node is not None and target_node.type in ("identifier", "qualified_name"):
                text = target_node.text.decode("utf-8")
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_ruby_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``require``/``require_relative`` targets from a Ruby AST.

    ``require_relative`` is workspace-relative (lexically resolved, extension-
    stripping limited to ``.rb`` — Ruby specifiers are typically already
    extensionless or carry ``.rb`` explicitly). Plain ``require`` defaults to
    bare/external (gem convention, mirrors Node's ``require()`` default for a
    bare specifier).
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            method = node.child_by_field_name("method")
            if method is not None and method.text in (b"require", b"require_relative"):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    string_node = next((c for c in args.children if c.type == "string"), None)
                    if string_node is not None:
                        text = _string_literal_text(string_node)
                        if text:
                            if method.text == b"require_relative":
                                target = _resolve_relative_specifier(text, req, strip_exts=(".rb",))
                            else:
                                target = text
                            if target and target not in seen:
                                seen.add(target)
                                out.append(target)
        stack.extend(reversed(node.children))
    return out


def _extract_lua_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``require(...)`` targets from a Lua AST.

    Lua module specifiers are dot-separated by convention
    (``require("pkg.sub")``) and resolve via the dotted suffix index, not
    path-relative math.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_call":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier" and name.text == b"require":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    string_node = next((c for c in args.children if c.type == "string"), None)
                    if string_node is not None:
                        content = string_node.child_by_field_name("content")
                        text = (
                            content.text.decode("utf-8")
                            if content is not None
                            else _string_literal_text(string_node)
                        )
                        if text and text not in seen:
                            seen.add(text)
                            out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_zig_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``@import(...)`` targets from a Zig AST.

    Same call-with-string-argument shape as JS's ``require()``/Lua's
    ``require()``. A relative specifier (``./foo.zig``) is lexically resolved
    (extension kept, already explicit); a bare one (``std``) stays external.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "builtin_function":
            ident = next((c for c in node.children if c.type == "builtin_identifier"), None)
            if ident is not None and ident.text == b"@import":
                args = next((c for c in node.children if c.type == "arguments"), None)
                if args is not None:
                    string_node = next((c for c in args.children if c.type == "string"), None)
                    if string_node is not None:
                        text = _string_literal_text(string_node)
                        if text:
                            if text.startswith("."):
                                target = _resolve_relative_specifier(text, req, strip_exts=())
                            else:
                                target = text
                            if target and target not in seen:
                                seen.add(target)
                                out.append(target)
        stack.extend(reversed(node.children))
    return out


def _extract_elixir_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``import``/``alias``/``require``/``use`` targets from an Elixir AST.

    ``call``'s field ``target`` names the macro; its ``arguments`` child is
    either a single ``alias`` node (the dotted target's own text) or a ``dot``
    node (field ``left`` = prefix, field ``right`` -> ``tuple`` of ``alias``
    children — a grouped ``alias MyApp.{Bar, Baz}`` — expanded into N separate
    targets). Reliability tracks the ``mix``-generated module-to-file
    convention, not a compiler guarantee (medium recall, like Kotlin/C#).
    """
    out: List[str] = []
    seen: set[str] = set()

    def _emit(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    macros = (b"import", b"alias", b"require", b"use")
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            target = node.child_by_field_name("target")
            if target is not None and target.type == "identifier" and target.text in macros:
                args = next((c for c in node.children if c.type == "arguments"), None)
                if args is not None:
                    for child in args.children:
                        if child.type == "alias":
                            _emit(child.text.decode("utf-8"))
                        elif child.type == "dot":
                            left = child.child_by_field_name("left")
                            right = child.child_by_field_name("right")
                            prefix = left.text.decode("utf-8") if left is not None else ""
                            if right is not None and right.type == "tuple":
                                for member in right.children:
                                    if member.type == "alias":
                                        _emit(f"{prefix}.{member.text.decode('utf-8')}")
        stack.extend(reversed(node.children))
    return out


def _extract_haskell_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Haskell AST.

    ``import``'s field ``module`` ALWAYS names the real target — even in the
    ``qualified ... as Alias`` form, where the field-named ``alias`` child is a
    SEPARATE ``module`` node holding the local alias (the grammar itself
    disambiguates by field name — no positional guessing needed here). A
    ``module``'s full text already concatenates its dotted chain. GHC's
    search-path convention is closely followed in practice — expect high
    recall, similar to Java.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import":
            module = node.child_by_field_name("module")
            if module is not None:
                text = module.text.decode("utf-8")
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_bash_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract ``source``/``.`` targets from a Bash AST.

    Filters on the exact ``command_name`` text (``source`` or ``.``) — an
    unambiguous, AST-visible match, never a raw-text regex that could
    false-positive inside an unrelated string. The path argument (field
    ``argument``) is lexically resolved with extension-stripping disabled (a
    sourced script's extension, if any, is already explicit).
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "command":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "command_name":
                word = next((c for c in name.children if c.type == "word"), None)
                if word is not None and word.text in (b"source", b"."):
                    arg = node.child_by_field_name("argument")
                    if arg is not None:
                        text = (
                            _string_literal_text(arg)
                            if arg.type == "string"
                            else arg.text.decode("utf-8")
                        )
                        if text:
                            resolved = _resolve_relative_specifier(text, req, strip_exts=())
                            if resolved is not None and resolved not in seen:
                                seen.add(resolved)
                                out.append(resolved)
        stack.extend(reversed(node.children))
    return out


def _extract_powershell_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract dot-sourcing and ``Import-Module`` targets from a PowerShell AST.

    PowerShell is case-insensitive — ``Import-Module``/``import-module`` both
    match. Dot-sourcing (``. .\\foo.ps1``) folds the whole path into
    ``command_name_expr``'s own text (field ``command_name``);
    ``Import-Module`` puts the module name/path in ``command_elements`` (a bare
    module name, with no path separator, stays external — an installed
    PowerShell module, not a local file).
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "command":
            name_node = node.child_by_field_name("command_name")
            first_child = node.children[0] if node.children else None
            if first_child is not None and first_child.type == "command_invokation_operator":
                if name_node is not None:
                    # PowerShell's native separator is "\" — _resolve_relative_specifier
                    # expects forward-slash input (the TS/JS/posixpath convention).
                    text = name_node.text.decode("utf-8").replace("\\", "/")
                    if text:
                        resolved = _resolve_relative_specifier(text, req, strip_exts=())
                        if resolved is not None and resolved not in seen:
                            seen.add(resolved)
                            out.append(resolved)
            elif (
                name_node is not None
                and name_node.text.decode("utf-8", "ignore").lower() == "import-module"
            ):
                elements = node.child_by_field_name("command_elements")
                if elements is not None:
                    tok = next(
                        (c for c in elements.children if c.type in ("generic_token", "string")),
                        None,
                    )
                    if tok is not None:
                        text = (
                            _string_literal_text(tok)
                            if tok.type == "string"
                            else tok.text.decode("utf-8")
                        )
                        if text:
                            if "/" in text or "\\" in text or text.startswith("."):
                                target = _resolve_relative_specifier(
                                    text.replace("\\", "/"), req, strip_exts=()
                                )
                            else:
                                target = text  # bare module name — external
                            if target and target not in seen:
                                seen.add(target)
                                out.append(target)
        stack.extend(reversed(node.children))
    return out


def _extract_swift_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import targets from a Swift AST.

    Extraction is trivial (``import_declaration`` -> ``identifier`` ->
    ``simple_identifier``), but the result is ALWAYS bare/external: Swift's
    implicit whole-module visibility means files within the same module never
    explicitly import each other, so ``import Foo`` names an external
    framework/package, never a sibling ``.swift`` file. This contributes
    external-dependency metadata only, by design — not a resolver gap.
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            ident = next((c for c in node.children if c.type == "identifier"), None)
            if ident is not None:
                text = ident.text.decode("utf-8")
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        stack.extend(reversed(node.children))
    return out


def _extract_php_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract require/include and namespace-``use`` targets from a PHP AST.

    ``require``/``require_once``/``include``/``include_once`` each wrap an
    ``encapsed_string`` (directly, or inside a ``parenthesized_expression`` for
    the call-style form) — resolved the same way as Ruby's ``require_relative``/
    C's quoted ``#include``, extension kept since already explicit.
    ``use Foo\\Bar;`` namespace imports resolve via the dotted/namespaced suffix
    index (separator ``\\``) — PSR-4 autoloading (Composer) maps a namespace to a
    directory closely enough that this tracks Java-like reliability for
    Composer-based projects.
    """
    out: List[str] = []
    seen: set[str] = set()

    def _emit(text: Optional[str]) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    include_kinds = {
        "require_expression", "require_once_expression",
        "include_expression", "include_once_expression",
    }
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in include_kinds:
            string_node = next(
                (c for c in node.children if c.type in ("encapsed_string", "string")), None
            )
            if string_node is None:
                paren = next(
                    (c for c in node.children if c.type == "parenthesized_expression"), None
                )
                if paren is not None:
                    string_node = next(
                        (c for c in paren.children if c.type in ("encapsed_string", "string")),
                        None,
                    )
            if string_node is not None:
                text = _string_literal_text(string_node)
                if text:
                    _emit(_resolve_relative_specifier(text, req, strip_exts=()))
        elif node.type == "namespace_use_declaration":
            for clause in node.children:
                if clause.type == "namespace_use_clause":
                    target = next(
                        (c for c in clause.children if c.type == "qualified_name"), None
                    )
                    if target is not None:
                        _emit(target.text.decode("utf-8"))
        stack.extend(reversed(node.children))
    return out


def _extract_dart_imports(tree: Any, req: IndexingRequest) -> list[str]:
    """Extract import/export targets from a Dart AST.

    Three resolution shapes: ``dart:core`` (built-in, always bare/external),
    ``package:foo/bar.dart`` (URI-scheme — the ``package:`` prefix is stripped
    and the remainder emitted as a package-relative path; whether it then
    resolves depends on whether the project's own indexed layout happens to
    match, since pubspec-aware package-name resolution is out of scope here),
    and a bare relative specifier (``'sibling.dart'``, resolved the same way as
    Ruby's ``require_relative``/C's quoted ``#include``, extension kept since
    already explicit).
    """
    out: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "uri":
            string_lit = next((c for c in node.children if c.type == "string_literal"), None)
            if string_lit is not None:
                text = _string_literal_text(string_lit)
                if text:
                    if text.startswith("dart:"):
                        target: Optional[str] = text
                    elif text.startswith("package:"):
                        target = text[len("package:"):]
                    else:
                        target = _resolve_relative_specifier(text, req, strip_exts=())
                    if target and target not in seen:
                        seen.add(target)
                        out.append(target)
        stack.extend(reversed(node.children))
    return out


# Import-edge extractors keyed by VS Code languageId. Dispatch is O(1); an
# unregistered language yields no edges (best-effort, mirroring the worker's
# never-raise contract).
IMPORT_EXTRACTORS: Dict[str, Callable[[Any, IndexingRequest], List[str]]] = {
    "python": _extract_python_imports,
    "typescript": _extract_ecmascript_imports,
    "typescriptreact": _extract_ecmascript_imports,
    "javascript": _extract_ecmascript_imports,
    "javascriptreact": _extract_ecmascript_imports,
    "c": _extract_c_family_includes,
    "cpp": _extract_c_family_includes,
    "rust": _extract_rust_imports,
    "go": _extract_go_imports,
    "java": _extract_java_imports,
    "kotlin": _extract_kotlin_imports,
    "scala": _extract_scala_imports,
    "csharp": _extract_csharp_imports,
    "ruby": _extract_ruby_imports,
    "lua": _extract_lua_imports,
    "zig": _extract_zig_imports,
    "elixir": _extract_elixir_imports,
    "haskell": _extract_haskell_imports,
    "shellscript": _extract_bash_imports,
    "powershell": _extract_powershell_imports,
    "swift": _extract_swift_imports,
    "php": _extract_php_imports,
    "dart": _extract_dart_imports,
}


def index_file_sync(req: IndexingRequest) -> IndexingResult:
    """Worker entry point: parse file AST, return a picklable result.

    Never raises — returns IndexingResult(success=False, error=...) on any exception
    so the asyncio caller always gets a result, never an unhandled worker exception.
    """
    global _worker_ast
    if _worker_ast is None:
        _worker_init()  # lazy fallback if pool was created without initializer
    ast_engine = _worker_ast
    if ast_engine is None:
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=0,
            language_id=req.language_id,
            success=False,
            error="AST engine unavailable",
        )
    try:
        tree = ast_engine.parse(
            req.file_path, req.content, req.language_id
        )
        imports: list[str] = []
        symbols: list[SymbolDef] = []
        if tree is not None:
            extractor = IMPORT_EXTRACTORS.get(req.language_id)
            if extractor is not None:
                imports = extractor(tree, req)
                # Symbol definitions share the import extractor's language scope: the
                # confidence-tiering anchor (import resolution) only exists for these
                # languages, so a generic walk elsewhere would buy noise, not signal.
                from core.ast_engine import collect_symbol_defs  # deferred: heavy tree-sitter dep
                symbols = [
                    SymbolDef(qualified_name=q, kind=k, start_line=s, end_line=e)
                    for q, k, s, e in collect_symbol_defs(tree.root_node)
                ]
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=_count_top_level_symbols(tree),
            language_id=req.language_id,
            success=True,
            imports=imports,
            symbols=symbols,
        )
    except Exception as exc:
        return IndexingResult(
            file_path=req.file_path,
            symbol_count=0,
            language_id=req.language_id,
            success=False,
            error=str(exc),
        )


def calculate_ppr_sync(req: PPRRequest) -> PPRResult:
    """Compute node centrality over the project dependency graph.

    CPU-bound — runs in ProcessPoolExecutor. Returns a centrality score for every
    node. Phase 3.3 uses this as the Graph_Centrality term in CSS. Uses pure-Python
    degree centrality (no scipy) so the runtime stays free of native C/Fortran
    extensions for lightweight bundling.
    """
    if len(req.edges) > MAX_GRAPH_EDGES:
        logger.warning(
            "Dependency graph exceeds the edge cap (%d > %d) — skipping centrality.",
            len(req.edges), MAX_GRAPH_EDGES,
        )
        return PPRResult(scores={}, success=True)
    G: Any = None
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(req.edges)
        if len(G) == 0:
            return PPRResult(scores={}, success=True)
        scores: dict[str, float] = nx.degree_centrality(G)
        return PPRResult(scores=scores, success=True)
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))
    finally:
        if G is not None:
            G.clear()


def _candidate_paths(normalized_target: str) -> Iterator[str]:
    """Lazily yield indexed-file candidates for an extensionless relative target.

    A TS/JS relative specifier resolves to an extensionless workspace path; the
    concrete file it names may carry any JS/TS extension or be a directory's
    ``index.*`` barrel. Yielding lazily lets the caller short-circuit on the first
    membership hit — no per-edge candidate list is materialized.
    """
    yield normalized_target
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        yield normalized_target + ext
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        yield normalized_target + "/index" + ext


def resolve_target_to_file(
    target: str, indexed: set[str], norm_indexed: Dict[str, str]
) -> Optional[str]:
    """Map a stored ``target_dependency`` to a concrete indexed file, or None.

    Direct membership first, then extension/``index.*`` candidate expansion for an
    extensionless TS/JS specifier. Pure string math over the pre-built indexed set and
    its forward-slash lookup (``norm_indexed``); no filesystem access. Shared by
    confidence scoring and the blast-radius mapper so both resolve edges identically.
    """
    if target in indexed:
        return target
    normalized_target = target.replace("\\", "/")
    hit = next((c for c in _candidate_paths(normalized_target) if c in norm_indexed), None)
    return norm_indexed[hit] if hit is not None else None


# Ecmascript specifiers never enter `FAMILY_TABLE` (they resolve via
# `resolve_target_to_file`'s path-style candidate expansion instead), but their
# basename-stem AMBIGUOUS check still needs its own scope bucket, distinct from
# every dotted-family language, to avoid a same-named file in an unrelated
# language falsely inflating this family's AMBIGUOUS rate (and vice versa).
_ECMASCRIPT_EXTS: Tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _stem_scope_key(f: str) -> str:
    """Bucket key for the basename-stem AMBIGUOUS check — one bucket per language
    family (never merged across languages, mirroring `module_resolver`'s own
    per-family suffix-index isolation) plus a shared "ecmascript" bucket and a
    catch-all for every other/unregistered extension."""
    nf = f.replace("\\", "/")
    ext = os.path.splitext(nf)[1].lower()
    if ext in _ECMASCRIPT_EXTS:
        return "ecmascript"
    family = family_for_source(nf)
    return family if family is not None else ext


def _resolve_edge_confidence(
    edges: Tuple[Tuple[str, str], ...], indexed_files: Tuple[str, ...]
) -> Tuple[Tuple[str, str, str, float], ...]:
    """Derive a confidence label/score per edge from whole-graph resolution.

    EXTRACTED (1.0): the target resolves to an indexed source file — directly, or
    (for an extensionless relative TS/JS specifier) via extension/``index.*``
    candidate expansion against the indexed set; or (for a dotted/namespaced
    target — Python, Java, Kotlin, Scala, C#, Rust, Lua, Elixir, Haskell) via the
    shared ``core.module_resolver`` suffix index, scoped to the *source* file's own
    language family so an unrelated language's same-named file can never
    cross-resolve into the wrong target (Go is deliberately excluded from this
    tier — see below). AMBIGUOUS (0.25): the target's module stem matches ≥2
    indexed files WITHIN THE SAME FAMILY, or the family suffix index itself found
    ≥2 hits. INFERRED (0.5): everything else — an external/unindexed module.

    All resolution is in-memory string math over the indexed set; no filesystem access.
    """
    indexed = set(indexed_files)
    norm_indexed = {f.replace("\\", "/"): f for f in indexed_files}
    family_indices = build_all_family_indices(indexed_files)

    stems_by_scope: Dict[str, Counter[str]] = {}
    for f in indexed_files:
        scope = _stem_scope_key(f)
        stem = os.path.splitext(os.path.basename(f.replace("\\", "/")))[0]
        if stem:
            stems_by_scope.setdefault(scope, Counter())[stem] += 1

    out: List[Tuple[str, str, str, float]] = []
    for source, target in edges:
        resolved = resolve_target_to_file(target, indexed, norm_indexed)
        if resolved is not None:
            out.append((source, resolved, "EXTRACTED", 1.0))
            continue

        # Dotted/namespaced suffix-index tier — additive, only engaged when the
        # path-style attempt above already failed. Go stays INFERRED here by design
        # (a package target maps to many files, which doesn't fit the
        # one-file-per-EXTRACTED-edge model); its full directory-level resolution
        # still applies in `core.blast_radius`, which returns multiple files.
        family = family_for_source(source)
        if family is not None and family != "go":
            cfg = FAMILY_TABLE[family]
            hits = resolve_via_suffix_index(target, cfg.separator, family_indices[family])
            if len(hits) == 1:
                out.append((source, hits[0], "EXTRACTED", 1.0))
                continue
            if len(hits) >= 2:
                out.append((source, target, "AMBIGUOUS", 0.25))
                continue

        module_stem = target.replace("\\", "/").rsplit("/", 1)[-1].split(".")[-1]
        scope = _stem_scope_key(source)
        if stems_by_scope.get(scope, Counter()).get(module_stem, 0) >= 2:
            out.append((source, target, "AMBIGUOUS", 0.25))
        else:
            out.append((source, target, "INFERRED", 0.5))
    return tuple(out)


def calculate_graph_analytics_sync(req: PPRRequest) -> PPRResult:
    """Unified graph analytics over the project dependency graph (one DiGraph build).

    CPU-bound — runs in ProcessPoolExecutor. Computes degree centrality (pure-Python,
    no scipy), Louvain community detection (on the undirected projection, fixed seed
    for stable colors), and per-edge confidence. Supersedes calculate_ppr_sync on the
    batch path; the latter is retained for callers that only need scores.
    """
    if len(req.edges) > MAX_GRAPH_EDGES:
        logger.warning(
            "Dependency graph exceeds the edge cap (%d > %d) — skipping analytics.",
            len(req.edges), MAX_GRAPH_EDGES,
        )
        return PPRResult(scores={}, success=True)
    G: Any = None
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(req.edges)
        if len(G) == 0:
            return PPRResult(scores={}, success=True)

        # Pure-Python degree centrality (no scipy) — keeps the runtime free of
        # native C/Fortran extensions for lightweight bundling. Best-effort so a
        # centrality hiccup never sinks community detection or confidence.
        scores: dict[str, float] = {}
        try:
            scores = nx.degree_centrality(G)
        except Exception as exc:  # noqa: BLE001 — centrality is best-effort
            logger.warning("Degree centrality unavailable (non-fatal): %s", exc)

        # Louvain runs on the undirected projection, which transiently doubles
        # the graph in memory — bind it so it can be released deterministically
        # rather than waiting on GC in the reused pool worker.
        communities: Dict[str, int] = {}
        undirected: Any = None
        try:
            undirected = G.to_undirected()
            partition = nx.community.louvain_communities(undirected, seed=42)
            for idx, members in enumerate(partition):
                for node in members:
                    communities[node] = idx
        except Exception as exc:  # noqa: BLE001 — community detection is best-effort
            logger.warning("Louvain community detection failed (non-fatal): %s", exc)
        finally:
            if undirected is not None:
                undirected.clear()

        edge_confidence = _resolve_edge_confidence(req.edges, req.indexed_files)
        return PPRResult(
            scores=scores,
            success=True,
            communities=communities,
            edge_confidence=edge_confidence,
        )
    except Exception as exc:
        return PPRResult(scores={}, success=False, error=str(exc))
    finally:
        if G is not None:
            G.clear()


# ── Architecture-overview digest ──────────────────────────────────────────────
# Synthesizes the persisted graph analytics into one bounded orientation payload.
# Pure and picklable: it takes plain, already-relativized data and touches no I/O,
# so it stays inside this process-pool-safe module without pulling a DB dependency.

# Per-section output caps — the primary bound on digest size (token hygiene). A
# capped section reports its true ``total`` beside the truncated slice so a caller
# knows more exist.
_HOTSPOT_LIMIT: int = 20
_MODULE_LIMIT: int = 20
_CLUSTER_LIMIT: int = 15
_ENTRYPOINT_LIMIT: int = 25

# Basenames that mark a real application entrypoint. Deliberately excludes test
# files: a test module is not an architectural entrypoint. (The dead-code scanner
# uses a wider notion that counts tests — to avoid flagging them as orphans — which
# is the wrong semantics for an orientation digest.)
_ARCH_ENTRYPOINT_BASENAMES: frozenset[str] = frozenset(
    {"main.py", "__main__.py", "app.py", "manage.py", "cli.py", "wsgi.py", "asgi.py"}
)

_ROOT_MODULE_LABEL: str = "<root>"
_NO_EXTENSION_LABEL: str = "<none>"


def _empty_digest() -> Dict[str, object]:
    """A well-formed, zero-valued digest mirroring the populated shape exactly.

    Returned for both an empty/cold project and the tool's fail-open path so every
    consumer key (``digest["languages"]`` …) is always present with its correct
    type — an empty ``{}`` would raise ``KeyError`` in a downstream reader or gate.
    """
    return {
        "languages": {"total": 0, "top": []},
        "top_modules": {"total": 0, "top": []},
        "hotspots": {"total": 0, "top": []},
        "community_clusters": {"count": 0, "largest": []},
        "entrypoints": {"total": 0, "top": []},
        "graph_schema": {"indexed_files": 0, "edges": 0},
    }


def build_architecture_digest_sync(
    *,
    rel_files: Tuple[str, ...],
    top_ppr_rel: Tuple[Tuple[str, float], ...],
    community_ids: Tuple[int, ...],
    edge_count: int,
) -> Dict[str, object]:
    """Assemble a bounded, deterministic architecture-overview digest.

    All inputs are plain, workspace-relative, forward-slash data (relativized by the
    caller). Deterministic: every list is sorted by a total order and truncated to a
    module-constant cap, and each capped section carries its true ``total``.
    """
    if not rel_files and not top_ppr_rel and not community_ids:
        return _empty_digest()

    # Languages by file extension (deterministic: -count, then extension label).
    lang_counter: Counter[str] = Counter()
    for f in rel_files:
        _, ext = os.path.splitext(f)
        lang_counter[ext.lower() if ext else _NO_EXTENSION_LABEL] += 1
    lang_sorted = sorted(lang_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    languages = {
        "total": len(lang_sorted),
        "top": [{"language": ext, "count": n} for ext, n in lang_sorted[:_MODULE_LIMIT]],
    }

    # Top-level modules (first path segment; root files fold into one label).
    mod_counter: Counter[str] = Counter()
    for f in rel_files:
        mod_counter[f.split("/", 1)[0] if "/" in f else _ROOT_MODULE_LABEL] += 1
    mod_sorted = sorted(mod_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    top_modules = {
        "total": len(mod_sorted),
        "top": [{"module": m, "count": n} for m, n in mod_sorted[:_MODULE_LIMIT]],
    }

    # Hotspots — highest-centrality files (stable secondary sort by path).
    hot_sorted = sorted(top_ppr_rel, key=lambda ps: (-ps[1], ps[0]))
    hotspots = {
        "total": len(hot_sorted),
        "top": [{"file": p, "score": s} for p, s in hot_sorted[:_HOTSPOT_LIMIT]],
    }

    # Community clusters — sizes per Louvain id (largest first, id tie-break).
    cluster_counter: Counter[int] = Counter(community_ids)
    cluster_sorted = sorted(cluster_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    community_clusters = {
        "count": len(cluster_sorted),
        "largest": [{"id": cid, "size": n} for cid, n in cluster_sorted[:_CLUSTER_LIMIT]],
    }

    # Entrypoints — files whose basename marks an application entry (no test files).
    entry_files = sorted(
        f for f in rel_files if f.rsplit("/", 1)[-1] in _ARCH_ENTRYPOINT_BASENAMES
    )
    entrypoints = {
        "total": len(entry_files),
        "top": entry_files[:_ENTRYPOINT_LIMIT],
    }

    return {
        "languages": languages,
        "top_modules": top_modules,
        "hotspots": hotspots,
        "community_clusters": community_clusters,
        "entrypoints": entrypoints,
        "graph_schema": {"indexed_files": len(rel_files), "edges": edge_count},
    }
