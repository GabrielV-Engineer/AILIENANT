# ailienant-core/core/rules.py
# Phase 2.24 — Vigilia: mtime-cached .ailienant.json rule loader.

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("RULE_MANAGER")

_RULES_FILENAME = ".ailienant.json"
_DEFAULT_RULE = "Follow standard best practices."


class RuleManager:
    """Singleton: loads .ailienant.json with mtime-based cache invalidation.

    Project-local .ailienant.json takes precedence; missing file silently
    falls back to the default Vigilia rule.
    """

    _instance: Optional["RuleManager"] = None
    _cached_rules: dict
    _last_mtime: float
    _last_path: str

    def __new__(cls) -> "RuleManager":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._cached_rules = {}
            inst._last_mtime = 0.0
            inst._last_path = ""
            cls._instance = inst
        return cls._instance

    def get_combined_rules(self, project_path: str) -> str:
        """Return formatted rules string. Re-reads disk only when mtime changes."""
        if not project_path:
            return self._default_rules()

        rules_path = os.path.join(project_path, _RULES_FILENAME)

        if not os.path.exists(rules_path):
            logger.debug("Vigilia: no %s at %s — using default.", _RULES_FILENAME, rules_path)
            return self._default_rules()

        try:
            mtime = os.path.getmtime(rules_path)
        except OSError:
            return self._default_rules()

        if rules_path == self._last_path and mtime <= self._last_mtime and self._cached_rules:
            logger.debug("Vigilia: cache hit for %s.", rules_path)
            return self._format(self._cached_rules)

        try:
            with open(rules_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._cached_rules = data
            self._last_mtime = mtime
            self._last_path = rules_path
            logger.info("Vigilia: loaded rules from %s.", rules_path)
            return self._format(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Vigilia: failed to load %s — %s. Using default.", rules_path, exc)
            return self._default_rules()

    @staticmethod
    def _format(data: dict) -> str:
        rules: list = data.get("rules", [])
        if not rules:
            return RuleManager._default_rules()
        bullets = "\n".join(f"- {r}" for r in rules)
        return f"### Project Instructions:\n{bullets}"

    @staticmethod
    def _default_rules() -> str:
        return f"### Project Instructions:\n- {_DEFAULT_RULE}"

    def reset(self) -> None:
        """Reset cache — call from test teardown to isolate singleton state."""
        self._cached_rules = {}
        self._last_mtime = 0.0
        self._last_path = ""


rule_manager = RuleManager()
