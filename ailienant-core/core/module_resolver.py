"""core/module_resolver.py — shared dotted/directory suffix-index resolver.

Generalizes what was previously a private, Python-only mechanism
(``blast_radius._build_python_suffix_index``) into a per-language-family
primitive shared by both ``brain.memory._resolve_edge_confidence`` (persisted
confidence scoring) and ``core.blast_radius`` (the pre-apply reverse-adjacency
walk) — plus ``core.dead_code``, which already imports the blast-radius wrapper
names directly.

A dotted/namespaced import target (``brain.state``, ``com.foo.Bar``,
``std::io::Read``) carries no lexically recoverable absolute path — the import
root is a search-path entry no static extractor ever sees. Indexing every
candidate file by its path-suffix windows lets a dotted target resolve to a
concrete file in O(1) without walking the filesystem.

Suffix indices are always built **per language family, never merged** — a
Python ``services/state.py`` and an unrelated Rust ``services/state.rs`` would
otherwise register the identical suffix ``services/state``, letting an
unrelated language's import silently cross-resolve into the wrong file. The
caller selects which family's index to query using the *source* file's own
extension (``family_for_source``), never the shape of the target string alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# granularity="file": index registers file-stem suffixes (a target names one file).
# granularity="directory": index registers directory-path suffixes, with every file
# in a matching directory appended — a target may legitimately name a whole package
# (Go), so "one target -> many files" is a real result, not an over-match artifact.


@dataclass(frozen=True)
class FamilyConfig:
    extensions: Tuple[str, ...]
    separator: str
    granularity: str  # "file" | "directory"
    strip_basenames: Tuple[str, ...] = ()


# One table, one place — both ``brain.memory`` and ``core.blast_radius`` import this
# rather than each hand-rolling their own extension/separator knowledge a second time.
FAMILY_TABLE: Dict[str, FamilyConfig] = {
    "python": FamilyConfig(extensions=(".py",), separator=".", granularity="file", strip_basenames=("__init__",)),
    "java": FamilyConfig(extensions=(".java",), separator=".", granularity="file"),
    "kotlin": FamilyConfig(extensions=(".kt",), separator=".", granularity="file"),
    "scala": FamilyConfig(extensions=(".scala",), separator=".", granularity="file"),
    "csharp": FamilyConfig(extensions=(".cs",), separator=".", granularity="file"),
    "rust": FamilyConfig(extensions=(".rs",), separator="::", granularity="file"),
    "go": FamilyConfig(extensions=(".go",), separator="/", granularity="directory"),
    "lua": FamilyConfig(extensions=(".lua",), separator=".", granularity="file"),
    "elixir": FamilyConfig(extensions=(".ex", ".exs"), separator=".", granularity="file"),
    "haskell": FamilyConfig(extensions=(".hs",), separator=".", granularity="file"),
    "php": FamilyConfig(extensions=(".php",), separator="\\", granularity="file"),
}

_EXT_TO_FAMILY: Dict[str, str] = {
    ext: family for family, cfg in FAMILY_TABLE.items() for ext in cfg.extensions
}


def family_for_source(source_file: str) -> Optional[str]:
    """Map a source file's extension to its resolver family, or ``None`` if unscoped."""
    nf = source_file.replace("\\", "/")
    dot = nf.rfind(".")
    if dot == -1:
        return None
    return _EXT_TO_FAMILY.get(nf[dot:].lower())


def build_suffix_index(
    indexed_files: Tuple[str, ...],
    extensions: Tuple[str, ...],
    granularity: str,
    strip_basenames: Tuple[str, ...] = (),
) -> Dict[str, List[str]]:
    """Map every segment-aligned path suffix to the indexed file(s) it names.

    ``granularity="file"`` indexes by the file's own stem (a target names one file).
    ``granularity="directory"`` indexes by the file's containing directory (a target
    names a package — every file in that directory accumulates under the same keys).
    A suffix matching several files/directories maps to all of them: over-matching
    over-counts a blast-radius/confidence signal, the safe direction for both callers.
    """
    idx: Dict[str, List[str]] = {}
    for f in indexed_files:
        nf = f.replace("\\", "/")
        matched_ext = next((ext for ext in extensions if nf.endswith(ext)), None)
        if matched_ext is None:
            continue
        if granularity == "file":
            stem = nf[: -len(matched_ext)] if matched_ext else nf
            for base in strip_basenames:
                suffix = "/" + base
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            parts = stem.split("/")
        else:  # "directory"
            dirname = nf.rsplit("/", 1)[0] if "/" in nf else ""
            if not dirname:
                continue  # a root-level file has no directory segments to index
            parts = dirname.split("/")
        for i in range(len(parts)):
            idx.setdefault("/".join(parts[i:]), []).append(nf)
    return idx


def build_all_family_indices(
    indexed_files: Tuple[str, ...]
) -> Dict[str, Dict[str, List[str]]]:
    """Build every family's suffix index in a single pass over ``indexed_files``."""
    out: Dict[str, Dict[str, List[str]]] = {name: {} for name in FAMILY_TABLE}
    for f in indexed_files:
        nf = f.replace("\\", "/")
        dot = nf.rfind(".")
        if dot == -1:
            continue
        family = _EXT_TO_FAMILY.get(nf[dot:].lower())
        if family is None:
            continue
        cfg = FAMILY_TABLE[family]
        if cfg.granularity == "file":
            matched_ext = next((ext for ext in cfg.extensions if nf.endswith(ext)), None)
            if matched_ext is None:
                continue
            stem = nf[: -len(matched_ext)]
            for base in cfg.strip_basenames:
                suffix = "/" + base
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            parts = stem.split("/")
        else:
            dirname = nf.rsplit("/", 1)[0] if "/" in nf else ""
            if not dirname:
                continue
            parts = dirname.split("/")
        idx = out[family]
        for i in range(len(parts)):
            idx.setdefault("/".join(parts[i:]), []).append(nf)
    return out


def resolve_via_suffix_index(
    target: str, separator: str, suffix_index: Dict[str, List[str]]
) -> List[str]:
    """Resolve a dotted/namespaced ``target`` against a single family's suffix index."""
    key = target.replace(separator, "/") if separator != "/" else target
    return suffix_index.get(key, [])
