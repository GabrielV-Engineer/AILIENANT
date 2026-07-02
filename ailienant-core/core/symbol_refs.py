"""core/symbol_refs.py — lazy "who calls this symbol" resolver (Tier-2 substrate).

No call edges are stored anywhere. References are resolved on demand in two passes:

  1. **Narrow** — the FTS5 trigram line-index pre-filters the already-indexed catalog to
     files that may mention the symbol name (``fts_narrow_catalog`` returns a *superset*,
     never dropping a true match, and is scoped to the indexed set — so the search never
     touches vendor/build dirs and carries no shell/regex/subprocess surface).
  2. **AST-confirm** — each candidate is parsed and kept only when the name appears as a
     real identifier reference. A name inside a string/comment is not an identifier node,
     so lexical noise is excluded structurally; the definition site itself is excluded too.

Every confirmed caller is retained and tagged with a confidence tier
(``EXTRACTED`` / ``AMBIGUOUS`` / ``INFERRED``). Import-path evidence RANKS a caller but
NEVER discards one — a hard import gate would silently drop dynamic-dispatch callers
(e.g. a string-keyed tool dispatcher that never imports the tool class it invokes), which
is exactly the caller class this resolver exists to surface. Output is advisory and
READ_ONLY: an empty or unconfirmed result means "no callers found via this resolution
path", never "confirmed dead".
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

from core import blast_radius
from core.ast_engine import ASTEngine, _is_class_like, _is_function_like
from core.db import fts_narrow_catalog, get_symbol_definitions, list_indexed_files
from shared.contracts import detect_language

logger = logging.getLogger(__name__)

# Identifier grammar shared by the scoped languages (Python + TS/JS). A name that is not
# identifier-shaped never reaches a search — closes command-injection and regex-ReDoS
# avenues even though the FTS path is already parameterized (defense-in-depth, §6.2).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Node types that carry a plain identifier reference across the scoped grammars. A name
# inside a string/comment is NOT one of these types, so lexical noise is excluded by
# construction rather than by a fragile substring check.
_IDENTIFIER_NODE_TYPES = frozenset({
    "identifier",
    "property_identifier",
    "field_identifier",
    "shorthand_property_identifier",
    "type_identifier",
})

_READ_MAX_BYTES: int = 100_000  # mirrors the analyst/dead-code disk cap (token + RAM hygiene)
_PARSE_CONCURRENCY: int = 8     # bounded fan-out — cap peak RAM on a common-name query
_DEFAULT_TIMEOUT_S: float = 4.0
_DEFAULT_MAX_RESULTS: int = 100

_CONFIDENCE_RANK = {"EXTRACTED": 0, "AMBIGUOUS": 1, "INFERRED": 2}

# Dedicated parse cache so on-demand reference scans never pollute the ingest engine.
_engine = ASTEngine()


@dataclass(frozen=True)
class SymbolCaller:
    """One file confirmed to reference the symbol, with the line(s) and confidence tier."""
    file_path: str
    lines: Tuple[int, ...]
    confidence: str  # EXTRACTED | AMBIGUOUS | INFERRED


@dataclass
class SymbolCallersResult:
    """Advisory result. ``in_catalog=False`` with ``callers=[]`` means the symbol is not
    catalogued — NOT that it is unused. ``truncated`` marks a capped/timed-out scan."""
    qualified_name: str
    defined_in: List[str] = field(default_factory=list)
    in_catalog: bool = False
    callers: List[SymbolCaller] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    timed_out: bool = False


def _confirm_references(tree: Any, bare_name: str) -> Tuple[int, ...]:
    """1-indexed lines where ``bare_name`` occurs as a real identifier reference.

    Excludes occurrences inside strings/comments (not identifier nodes) and the
    definition-name site itself. Returns an empty tuple when there is no confirmed
    reference. Never raises — a malformed node is skipped.
    """
    root = getattr(tree, "root_node", None)
    if root is None:
        return ()
    def_name_starts: Set[int] = set()
    ref_lines: Set[int] = set()
    stack: List[Any] = [root]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        # A def node is always popped before its (descendant) name child, so the span is
        # recorded before the child identifier is tested against it.
        if _is_function_like(node) or _is_class_like(node):
            try:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    def_name_starts.add(nm.start_byte)
            except Exception:
                pass
        if (getattr(node, "type", "") or "") in _IDENTIFIER_NODE_TYPES:
            try:
                if node.text.decode("utf-8", "replace") == bare_name \
                        and node.start_byte not in def_name_starts:
                    ref_lines.add(node.start_point[0] + 1)
            except Exception:
                pass
        stack.extend(getattr(node, "children", None) or [])
    return tuple(sorted(ref_lines))


def _read_capped(path: str, project_id: str, workspace_root: str) -> Optional[str]:
    """Freshest bytes (RAM-VFS ∪ disk) for a candidate, workspace-jailed and byte-capped."""
    from core.vfs_middleware import VFSMiddleware  # deferred — avoid import cycle
    try:
        res = VFSMiddleware().read_safe(path, project_id=project_id, project_root=workspace_root)
    except Exception as exc:  # noqa: BLE001 — a single unreadable candidate must not abort the scan
        logger.debug("symbol_refs: VFS read error for %s: %s", path, exc)
        return None
    if not res.ok or res.content is None:
        return None
    return res.content[:_READ_MAX_BYTES]


def _confirm_in_content(path: str, content: str, language_id: str, bare_name: str) -> Tuple[int, ...]:
    """Parse one candidate and return the confirmed reference lines (off-loop worker)."""
    tree = _engine.parse(path, content, language_id)
    if tree is None:
        return ()
    return _confirm_references(tree, bare_name)


async def find_symbol_callers(
    qualified_name: str,
    project_id: str = "",
    *,
    workspace_root: str = "",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> SymbolCallersResult:
    """Resolve the files that reference ``qualified_name``. See module docstring.

    Raises ``ValueError`` when the bare symbol name is not identifier-shaped (input
    guard, §6.2). Never phrases an empty result as "dead": an uncatalogued symbol is
    reported via ``in_catalog=False``.
    """
    bare = qualified_name.rsplit(".", 1)[-1].strip()
    if not _IDENTIFIER_RE.match(bare):
        raise ValueError(f"symbol name {bare!r} is not identifier-shaped; refusing to search")

    # Step 0 — anchor the definition(s). Empty → proceed with no anchor (all INFERRED).
    defs = await get_symbol_definitions(project_id, qualified_name)
    defined_files = [d[0] for d in defs]
    in_catalog = bool(defs)

    # Direct importers of a defining file → the EXTRACTED anchor. Reuses the
    # blast-radius resolver (module/path aware) at depth 1.
    importers: Set[str] = set()
    if defined_files:
        # Module-attribute call so the resolver honours the same test stub the rest of
        # the codebase applies to blast-radius (hermetic units) while running the real
        # resolver in production.
        direct = await blast_radius.compute_blast_radius(
            project_id, defined_files, depth=1, workspace_root=workspace_root
        )
        importers = {p.replace("\\", "/") for p in direct}

    # Pass 1 — narrow to candidate files (superset; None → full catalog scan).
    catalog = await list_indexed_files(project_id)
    narrowed = await fts_narrow_catalog(project_id, bare, catalog)
    candidates = list(narrowed) if narrowed is not None else list(catalog)

    # Truncation-aware ordering: import-connected candidates first, so a deadline sheds
    # the low-value tail (unconnected same-name noise), never the high-confidence hit.
    candidates.sort(key=lambda p: (0 if p.replace("\\", "/") in importers else 1, p.replace("\\", "/")))

    deadline = time.monotonic() + max(0.1, timeout_s)
    sem = asyncio.Semaphore(_PARSE_CONCURRENCY)
    scanned = 0
    callers: List[SymbolCaller] = []

    async def _confirm(path: str) -> Optional[SymbolCaller]:
        nonlocal scanned
        async with sem:
            if time.monotonic() >= deadline:
                return None
            content = await asyncio.to_thread(_read_capped, path, project_id, workspace_root)
            if content is None:
                return None
            lang = detect_language(path)
            if not lang:
                return None
            lines = await asyncio.to_thread(_confirm_in_content, path, content, lang, bare)
            scanned += 1
            if not lines:
                return None
            npath = path.replace("\\", "/")
            if npath in importers:
                conf = "EXTRACTED"
            elif len(defined_files) >= 2:
                conf = "AMBIGUOUS"
            else:
                conf = "INFERRED"
            return SymbolCaller(file_path=path, lines=lines, confidence=conf)

    # Process in ranked order, bounded concurrency per batch, stopping at the deadline.
    idx, total = 0, len(candidates)
    while idx < total and time.monotonic() < deadline:
        batch = candidates[idx:idx + _PARSE_CONCURRENCY]
        idx += len(batch)
        for c in await asyncio.gather(*(_confirm(p) for p in batch)):
            if c is not None:
                callers.append(c)

    timed_out = idx < total and time.monotonic() >= deadline
    callers.sort(key=lambda c: (_CONFIDENCE_RANK.get(c.confidence, 9), c.file_path.replace("\\", "/")))
    truncated = idx < total or len(callers) > max_results
    return SymbolCallersResult(
        qualified_name=qualified_name,
        defined_in=defined_files,
        in_catalog=in_catalog,
        callers=callers[:max_results],
        scanned=scanned,
        truncated=truncated,
        timed_out=timed_out,
    )
