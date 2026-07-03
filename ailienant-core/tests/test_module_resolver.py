"""Shared dotted/directory suffix-index resolver (``core.module_resolver``).

Exercises ``build_suffix_index``/``build_all_family_indices``/``resolve_via_suffix_index``/
``family_for_source`` directly against seeded path tuples (no live DB, no tree-sitter):
- file-granularity suffix windows (the Python/Java/etc. shape) and directory-granularity
  suffix windows (the Go shape, one target -> many files),
- the ``strip_basenames`` Python ``__init__`` case reproduces the pre-8.14.10 private
  ``blast_radius._build_python_suffix_index`` behaviour byte-for-byte,
- the mandatory cross-language non-collision guarantee: two languages sharing an
  identical basename/suffix must never resolve into each other's file,
- ``family_for_source`` maps an extension to exactly one family or ``None``.
"""
from __future__ import annotations

from core.module_resolver import (
    FAMILY_TABLE,
    build_all_family_indices,
    build_suffix_index,
    family_for_source,
    resolve_via_suffix_index,
)


# ── build_suffix_index: file granularity ─────────────────────────────────────


def test_file_granularity_registers_every_suffix_window() -> None:
    idx = build_suffix_index(("/ws/pkg/brain/state.py",), extensions=(".py",), granularity="file")
    assert idx["pkg/brain/state"] == ["/ws/pkg/brain/state.py"]
    assert idx["brain/state"] == ["/ws/pkg/brain/state.py"]
    assert idx["state"] == ["/ws/pkg/brain/state.py"]
    assert "" not in idx  # range(len(parts)) never yields the empty suffix


def test_file_granularity_ignores_non_matching_extension() -> None:
    idx = build_suffix_index(("/ws/pkg/state.rs",), extensions=(".py",), granularity="file")
    assert idx == {}


def test_strip_basenames_reproduces_python_init_behaviour() -> None:
    idx = build_suffix_index(
        ("/ws/pkg/brain/__init__.py",), extensions=(".py",), granularity="file",
        strip_basenames=("__init__",),
    )
    assert idx["pkg/brain"] == ["/ws/pkg/brain/__init__.py"]
    assert idx["brain"] == ["/ws/pkg/brain/__init__.py"]
    assert "__init__" not in idx


def test_over_matching_suffix_maps_to_all_files() -> None:
    idx = build_suffix_index(
        ("/ws/a/state.py", "/ws/b/state.py"), extensions=(".py",), granularity="file",
    )
    assert sorted(idx["state"]) == ["/ws/a/state.py", "/ws/b/state.py"]


# ── build_suffix_index: directory granularity (Go's shape) ───────────────────


def test_directory_granularity_registers_directory_suffixes_not_file_stems() -> None:
    idx = build_suffix_index(
        ("/ws/pkg/util/a.go", "/ws/pkg/util/b.go"), extensions=(".go",), granularity="directory",
    )
    # Both files in the SAME directory accumulate under the same directory-suffix key —
    # a target names a whole package, not one file.
    assert sorted(idx["pkg/util"]) == ["/ws/pkg/util/a.go", "/ws/pkg/util/b.go"]
    assert sorted(idx["util"]) == ["/ws/pkg/util/a.go", "/ws/pkg/util/b.go"]
    # The file's own basename is never a key at directory granularity.
    assert "a" not in idx
    assert "util/a" not in idx


def test_directory_granularity_skips_root_level_file() -> None:
    idx = build_suffix_index(("/main.go",), extensions=(".go",), granularity="directory")
    assert idx == {}  # no directory segment to index


# ── build_all_family_indices: single-pass, per-family isolation ──────────────


def test_build_all_family_indices_covers_every_registered_family() -> None:
    indexed = ("/ws/a.py", "/ws/b.java", "/ws/c.rs")
    all_idx = build_all_family_indices(indexed)
    assert set(all_idx) == set(FAMILY_TABLE)
    assert all_idx["python"]["a"] == ["/ws/a.py"]
    assert all_idx["java"]["b"] == ["/ws/b.java"]
    assert all_idx["rust"]["c"] == ["/ws/c.rs"]


def test_cross_language_suffix_collision_never_leaks_across_families() -> None:
    """The mandatory correctness fix: a Python and a Rust file sharing the exact
    same basename-suffix must resolve ONLY within their own family's index."""
    indexed = ("/ws/services/state.py", "/ws/services/state.rs")
    all_idx = build_all_family_indices(indexed)
    py_hits = resolve_via_suffix_index("services.state", ".", all_idx["python"])
    rust_hits = resolve_via_suffix_index("services::state", "::", all_idx["rust"])
    assert py_hits == ["/ws/services/state.py"]
    assert rust_hits == ["/ws/services/state.rs"]
    # Neither family's index contains the other language's file at all.
    assert "/ws/services/state.rs" not in all_idx["python"].get("services/state", [])
    assert "/ws/services/state.py" not in all_idx["rust"].get("services/state", [])


# ── resolve_via_suffix_index: separator handling ──────────────────────────────


def test_resolve_via_suffix_index_dotted_separator() -> None:
    idx = {"brain/state": ["/ws/brain/state.py"]}
    assert resolve_via_suffix_index("brain.state", ".", idx) == ["/ws/brain/state.py"]


def test_resolve_via_suffix_index_double_colon_separator() -> None:
    idx = {"crate/foo/bar": ["/ws/crate/foo/bar.rs"]}
    assert resolve_via_suffix_index("crate::foo::bar", "::", idx) == ["/ws/crate/foo/bar.rs"]


def test_resolve_via_suffix_index_native_slash_separator_is_noop() -> None:
    idx = {"pkg/util": ["/ws/pkg/util/a.go"]}
    assert resolve_via_suffix_index("pkg/util", "/", idx) == ["/ws/pkg/util/a.go"]


def test_resolve_via_suffix_index_no_hit_returns_empty() -> None:
    assert resolve_via_suffix_index("nonexistent.mod", ".", {}) == []


# ── family_for_source ─────────────────────────────────────────────────────────


def test_family_for_source_maps_known_extensions() -> None:
    assert family_for_source("/ws/a.py") == "python"
    assert family_for_source("/ws/A.java") == "java"
    assert family_for_source("/ws/a.rs") == "rust"
    assert family_for_source("/ws/main.go") == "go"


def test_family_for_source_case_insensitive() -> None:
    assert family_for_source("/ws/A.PY") == "python"


def test_family_for_source_unknown_extension_returns_none() -> None:
    assert family_for_source("/ws/a.ts") is None  # ecmascript is not in FAMILY_TABLE
    assert family_for_source("/ws/noext") is None
