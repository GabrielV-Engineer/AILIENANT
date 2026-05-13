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
        content_hash = hashlib.blake2b(content.encode(), digest_size=16).hexdigest()
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
