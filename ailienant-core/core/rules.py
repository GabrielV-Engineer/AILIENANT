# ailienant-core/core/rules.py
"""Phase 3.4.6 — Dual-Rules Resolver.

Hierarchical .ailienant.json resolution:
  Global: ~/.ailienant/.ailienant.json
  Local:  <workspace>/.ailienant/.ailienant.json  (subdirectory preferred)
          fallback to <workspace>/.ailienant.json (flat, backwards-compat)

Composition: list-shape rules concatenate+dedupe with local first;
dict-shape rules deep-merge with local override. Cache is invalidated when
the mtime of any candidate file changes.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("RULE_MANAGER")

_RULES_FILENAME: str = ".ailienant.json"
_RULES_SUBDIR: str = ".ailienant"
_DEFAULT_RULE: str = "Follow standard best practices."

CacheKey = Tuple[Tuple[str, float], ...]


class RuleManager:
    """Singleton: loads dual-scope .ailienant.json with mtime-based cache.

    Local rules override global on conflicts. Both lists and dicts are supported
    at the top level for the `rules` key.
    """

    _instance: Optional["RuleManager"] = None
    _cache_key: Optional[CacheKey]
    _cached_formatted: str

    def __new__(cls) -> "RuleManager":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._cache_key = None
            inst._cached_formatted = ""
            cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_combined_rules(self, project_path: str) -> str:
        """Return formatted rules merging global + local. Re-reads only on mtime change."""
        candidates: List[Tuple[str, Path]] = self._candidate_paths(project_path)

        # Compose cache key: sorted (path_str, mtime) pairs for currently-existing files.
        new_key: CacheKey = tuple(sorted(
            (str(p), self._safe_mtime(p)) for _, p in candidates
        ))

        if new_key == self._cache_key and self._cached_formatted:
            logger.debug("RuleManager: cache hit (key=%s).", new_key)
            return self._cached_formatted

        local_data: Dict[str, Any] = {}
        global_data: Dict[str, Any] = {}
        for label, path in candidates:
            loaded: Optional[Dict[str, Any]] = self._load_one(path)
            if loaded is None:
                continue
            if label == "local":
                local_data = loaded
            else:
                global_data = loaded

        merged: Dict[str, Any] = self._compose(local_data, global_data)
        formatted: str = self._format(merged)
        self._cache_key = new_key
        self._cached_formatted = formatted
        logger.info(
            "RuleManager: composed rules (local_keys=%d global_keys=%d).",
            len(local_data), len(global_data),
        )
        return formatted

    def reset(self) -> None:
        """Reset cache — call from test teardown to isolate singleton state."""
        self._cache_key = None
        self._cached_formatted = ""

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _candidate_paths(project_path: str) -> List[Tuple[str, Path]]:
        """Return (label, path) pairs for currently-existing rule files.

        Order: local-subdir OR local-flat (whichever exists, subdir preferred),
        then global. Only existing files are returned so we never `open()` a
        nonexistent path; mtimes also come from these only.
        """
        out: List[Tuple[str, Path]] = []
        if project_path:
            local_sub: Path = Path(project_path) / _RULES_SUBDIR / _RULES_FILENAME
            local_flat: Path = Path(project_path) / _RULES_FILENAME
            if local_sub.is_file():
                out.append(("local", local_sub))
            elif local_flat.is_file():
                out.append(("local", local_flat))
        global_path: Path = Path.home() / _RULES_SUBDIR / _RULES_FILENAME
        if global_path.is_file():
            out.append(("global", global_path))
        return out

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return os.path.getmtime(str(path))
        except OSError:
            return 0.0

    @staticmethod
    def _load_one(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(str(path), "r", encoding="utf-8") as fh:
                data: Any = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("RuleManager: failed to load %s — %s.", path, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("RuleManager: %s did not contain a JSON object; ignoring.", path)
            return None
        return data

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    @classmethod
    def _compose(
        cls,
        local: Dict[str, Any],
        global_: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge local + global. Local always wins on conflict.

        Special-case `rules`: list/list → concat+dedupe local-first; dict/dict →
        deep-merge; mismatched shapes → local wins entirely.
        Other top-level keys deep-merge.
        """
        if not local and not global_:
            return {}
        if not local:
            return dict(global_)
        if not global_:
            return dict(local)

        out: Dict[str, Any] = {}
        keys: set[str] = set(local.keys()) | set(global_.keys())
        for k in keys:
            lv: Any = local.get(k)
            gv: Any = global_.get(k)
            if k == "rules":
                out[k] = cls._merge_rules_value(lv, gv)
            else:
                out[k] = cls._deep_merge(lv, gv)
        return out

    @classmethod
    def _merge_rules_value(cls, local_value: Any, global_value: Any) -> Any:
        # Both lists -> concat+dedupe local-first.
        if isinstance(local_value, list) and isinstance(global_value, list):
            return cls._concat_dedupe(local_value, global_value)
        # Both dicts -> deep merge.
        if isinstance(local_value, dict) and isinstance(global_value, dict):
            return cls._deep_merge(local_value, global_value)
        # One side missing -> use the present side.
        if local_value is None:
            return global_value
        if global_value is None:
            return local_value
        # Mismatched shapes -> local wins, warn.
        logger.warning(
            "RuleManager: mismatched 'rules' types (local=%s, global=%s); local wins.",
            type(local_value).__name__, type(global_value).__name__,
        )
        return local_value

    @classmethod
    def _deep_merge(cls, local_value: Any, global_value: Any) -> Any:
        """Recursive merge. Dicts are merged key-by-key with local override; lists
        concat+dedupe with local-first; scalars: local wins unless None."""
        if isinstance(local_value, dict) and isinstance(global_value, dict):
            out: Dict[str, Any] = {}
            for k in set(local_value.keys()) | set(global_value.keys()):
                out[k] = cls._deep_merge(local_value.get(k), global_value.get(k))
            return out
        if isinstance(local_value, list) and isinstance(global_value, list):
            return cls._concat_dedupe(local_value, global_value)
        if local_value is None:
            return global_value
        return local_value

    @staticmethod
    def _concat_dedupe(local_list: List[Any], global_list: List[Any]) -> List[Any]:
        out: List[Any] = list(local_list)
        for item in global_list:
            if item not in out:
                out.append(item)
        return out

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @classmethod
    def _format(cls, data: Dict[str, Any]) -> str:
        rules: Any = data.get("rules")
        header: str = "### Project Instructions:"

        if isinstance(rules, list) and rules:
            bullets: str = "\n".join(f"- {r}" for r in rules)
            return f"{header}\n{bullets}"

        if isinstance(rules, dict) and rules:
            lines: List[str] = []
            for key, value in rules.items():
                if isinstance(value, (dict, list)):
                    lines.append(f"- {key}: {json.dumps(value, separators=(',', ':'))}")
                else:
                    lines.append(f"- {key}: {value}")
            return f"{header}\n" + "\n".join(lines)

        return cls._default_rules()

    @staticmethod
    def _default_rules() -> str:
        return f"### Project Instructions:\n- {_DEFAULT_RULE}"


rule_manager: RuleManager = RuleManager()
