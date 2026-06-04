import hashlib
import importlib
import logging
import threading
from typing import Any, Dict, Optional, Tuple

from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

# --- Mapeo de Identificadores de VS Code a Gramáticas Tree-sitter ---
# ¿Por qué este mapeo?:
# 1. VS Code utiliza languageId específicos (ej. 'typescriptreact') que Tree-sitter
#    no reconoce de forma nativa — este dict actúa como el "Traductor de Estructura".
# 2. Permite a la IA diseccionar el código en nodos lógicos (funciones, clases, loops)
#    en lugar de tratar el código como simple texto plano.
# 3. Soporte multi-dominio: Web, Systems (C/C++/Zig), Enterprise, DevOps, Data Science.
# 4. Lazy Loading: los parsers solo se cargan en RAM cuando se abre un archivo de ese tipo.
# Nota: lenguas no soportadas por tree-sitter-languages son capturadas por el try/except
# en parse() y retornan None (degradación grácil — sin fallos).
_LANG_MAP: Dict[str, str] = {
    # Núcleo Web & Scripts
    "python": "python",
    "typescript": "typescript",
    "typescriptreact": "tsx",
    "javascript": "javascript",
    "javascriptreact": "javascript",
    "json": "json",
    "yaml": "yaml",

    # Sistemas & Rendimiento (C-Family)
    "c": "c",
    "cpp": "cpp",
    "rust": "rust",
    "go": "go",
    "zig": "zig",

    # Enterprise & Mobile
    "java": "java",
    "csharp": "c_sharp",
    "kotlin": "kotlin",
    "swift": "swift",
    "dart": "dart",

    # Backend & Programación Funcional
    "php": "php",
    "ruby": "ruby",
    "elixir": "elixir",
    "scala": "scala",
    "haskell": "haskell",

    # Automatización, Datos & Scripting
    "shellscript": "bash",
    "powershell": "powershell",
    "r": "r",
    "lua": "lua",
    "sql": "sql",
}

# (python_module_name, function_name) for each tree-sitter grammar key.
# TypeScript is special: one package exposes two separate functions.
# Entries missing here degrade gracefully (None returned, never crash).
_LANG_SOURCES: Dict[str, Tuple[str, str]] = {
    "python":     ("tree_sitter_python",     "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx":        ("tree_sitter_typescript", "language_tsx"),
    "json":       ("tree_sitter_json",       "language"),
    "yaml":       ("tree_sitter_yaml",       "language"),
    "c":          ("tree_sitter_c",          "language"),
    "cpp":        ("tree_sitter_cpp",        "language"),
    "rust":       ("tree_sitter_rust",       "language"),
    "go":         ("tree_sitter_go",         "language"),
    "zig":        ("tree_sitter_zig",        "language"),
    "java":       ("tree_sitter_java",       "language"),
    "c_sharp":    ("tree_sitter_c_sharp",    "language"),
    "kotlin":     ("tree_sitter_kotlin",     "language"),
    "swift":      ("tree_sitter_swift",      "language"),
    "ruby":       ("tree_sitter_ruby",       "language"),
    "elixir":     ("tree_sitter_elixir",     "language"),
    "scala":      ("tree_sitter_scala",      "language"),
    "haskell":    ("tree_sitter_haskell",    "language"),
    "bash":       ("tree_sitter_bash",       "language"),
    "powershell": ("tree_sitter_powershell", "language"),
    "lua":        ("tree_sitter_lua",        "language"),
    "sql":        ("tree_sitter_sql",        "language"),
}

# Cache of Language objects — expensive to create, cheap to reuse
_lang_cache: Dict[str, Language] = {}
_lang_cache_lock = threading.Lock()


def ast_content_hash(content: str) -> str:
    """blake2b content digest — the shared key primitive.

    Both the AST tree cache (re-parse only on content change) and the semantic
    response cache key off this exact digest, so a single function owns the
    "did the bytes change?" decision across both subsystems.
    """
    return hashlib.blake2b(content.encode(), digest_size=16).hexdigest()


def _get_language(lang_key: str) -> Optional[Language]:
    with _lang_cache_lock:
        if lang_key in _lang_cache:
            return _lang_cache[lang_key]
    source = _LANG_SOURCES.get(lang_key)
    if source is None:
        return None
    pkg_name, func_name = source
    try:
        pkg = importlib.import_module(pkg_name)
        lang = Language(getattr(pkg, func_name)())
        with _lang_cache_lock:
            _lang_cache[lang_key] = lang
        return lang
    except Exception as exc:
        logger.debug("Language load failed for %s (%s.%s): %s", lang_key, pkg_name, func_name, exc)
        return None


class ASTEngine:
    """
    Content-hash-keyed in-memory AST cache.
    Re-parses only when file content changes; returns cached tree otherwise.
    Thread-safe — safe for concurrent ingest calls from FastAPI.
    """

    def __init__(self) -> None:
        # path → (content_hash, tree)
        self._cache: Dict[str, Tuple[str, Any]] = {}
        self._lock = threading.Lock()

    def parse(self, path: str, content: str, language_id: str) -> Optional[Any]:
        lang_key = _LANG_MAP.get(language_id)
        if lang_key is None:
            return None
        content_hash = ast_content_hash(content)
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == content_hash:
                return cached[1]
        language = _get_language(lang_key)
        if language is None:
            return None
        try:
            tree = Parser(language).parse(bytes(content, "utf-8"))
        except Exception as exc:
            logger.debug("AST parse failed for %s: %s", path, exc)
            return None
        with self._lock:
            self._cache[path] = (content_hash, tree)
        return tree

    def get(self, path: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(path)
            return entry[1] if entry else None

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)


# --- Code-STYLE skeleton distillation (Few-Shot exemplars) ---
# Why: feeding the coder whole functions as style exemplars leaks logic (invites
# copy-paste) and burns tokens. A skeleton keeps the part that teaches house
# convention — signature, type hints, docstring — and elides the body to '...'.
# The function body hangs off the function node under the field name "body" across
# every tree-sitter grammar (python, JS/TS, Rust, Go, Java, C, …), so a single
# polyglot idiom drives the elision — no per-grammar body-type table.

_SKELETON_MAX_BYTES: int = 1500
_FUNCTION_NODE_HINTS: Tuple[str, ...] = ("function", "method", "constructor")
_SKELETON_PARSE_KEY: str = "<skeleton-distill>"

# Dedicated cache so transient snippet parses never pollute the ingest engine.
_skeleton_engine = ASTEngine()


def _is_function_like(node: Any) -> bool:
    """A node that names a callable and exposes a 'body' field is a function."""
    type_name = getattr(node, "type", "") or ""
    if not any(hint in type_name for hint in _FUNCTION_NODE_HINTS):
        return False
    try:
        return node.child_by_field_name("body") is not None
    except Exception:
        return False


def _collect_function_nodes(root: Any) -> list[Any]:
    """Pre-order walk yielding function-like nodes in source order.

    Function bodies are NOT descended into, so nested closures are not listed
    separately (their enclosing skeleton already elides them).
    """
    found: list[Any] = []
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if _is_function_like(node):
            found.append(node)
            continue  # do not descend into the body we are about to elide
        children = getattr(node, "children", None) or []
        stack.extend(reversed(children))
    return found


def _leading_comment(func: Any) -> str:
    """Contiguous doc-comment sibling(s) immediately above the function (JSDoc, ///).

    Bounded to comments that sit directly above (≤1 newline gap) so module-level
    license headers are never swept in.
    """
    try:
        comments: list[str] = []
        prev = func.prev_sibling
        anchor_start = func.start_byte
        while prev is not None and prev.type == "comment":
            if anchor_start - prev.end_byte > 2:  # blank-line gap → unrelated comment
                break
            comments.append(prev.text.decode("utf-8", "replace"))
            anchor_start = prev.start_byte
            prev = prev.prev_sibling
        return "\n".join(reversed(comments))
    except Exception:
        return ""


def _python_docstring(body: Any) -> str:
    """The body's leading string literal, if any (Python docstring)."""
    try:
        named = [c for c in body.named_children if c.type != "comment"]
        if not named:
            return ""
        first = named[0]
        if (
            first.type == "expression_statement"
            and first.named_children
            and first.named_children[0].type == "string"
        ):
            return str(first.named_children[0].text.decode("utf-8", "replace"))
    except Exception:
        return ""
    return ""


def _function_skeleton(func: Any, language_id: str) -> str:
    """Signature (+ Python docstring) with the body elided to '...'.

    Operates on the decoded function text so indentation is never corrupted by
    bare byte-pointer arithmetic. Returns '' for any malformed/body-less node.
    """
    try:
        body = func.child_by_field_name("body")
        if body is None:
            return ""
        sig_len = body.start_byte - func.start_byte
        if sig_len <= 0:
            return ""
        signature = func.text[:sig_len].decode("utf-8", "replace").rstrip()
        if not signature:
            return ""
    except Exception:
        return ""

    if language_id == "python":
        doc = _python_docstring(body)
        skeleton = f"{signature}\n    {doc}\n    ..." if doc else f"{signature}\n    ..."
    else:
        skeleton = f"{signature} {{ ... }}"

    comment = _leading_comment(func)
    return f"{comment}\n{skeleton}" if comment else skeleton


def extract_skeleton(content: str, language_id: str) -> str:
    """Distill source into function skeletons (signature + docstring, body → '...').

    Reuses the polyglot tree-sitter engine. Best-effort: returns '' on empty
    input, unsupported language, parse failure, or any unexpected node shape —
    never raises. Output is hard-capped at _SKELETON_MAX_BYTES for token safety.
    """
    if not content.strip():
        return ""
    try:
        tree = _skeleton_engine.parse(_SKELETON_PARSE_KEY, content, language_id)
        if tree is None:
            return ""
        skeletons = [
            skel
            for func in _collect_function_nodes(tree.root_node)
            if (skel := _function_skeleton(func, language_id))
        ]
        if not skeletons:
            return ""
        joined = "\n\n".join(skeletons)
        encoded = joined.encode("utf-8")
        if len(encoded) > _SKELETON_MAX_BYTES:
            joined = encoded[:_SKELETON_MAX_BYTES].decode("utf-8", "ignore").rstrip()
        return joined
    except Exception as exc:
        logger.debug("extract_skeleton failed (non-fatal): %s", exc)
        return ""
