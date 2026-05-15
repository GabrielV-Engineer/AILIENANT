# ailienant-core/tests/test_rules.py
#
# Phase 2.24 DoD: pytest tests/test_rules.py -v -> 0 failures.
#
# Coverage:
#   1. RuleManager loads .ailienant.json and formats correctly
#   2. Cache: second call without mtime change skips disk read
#   3. Integration: rules appear in the assembled planner system prompt

import json

import pytest

from core.rules import rule_manager
from prompts import build_safe_prompt
from shared.rbac import PLANNER_IDENTITY


@pytest.fixture(autouse=True)
def reset_rule_manager():
    """Isolate singleton cache between tests."""
    rule_manager.reset()
    yield
    rule_manager.reset()


def test_loads_local_rules_file(tmp_path):
    """.ailienant.json rules are formatted as '### Project Instructions:\\n- rule'."""
    rules_file = tmp_path / ".ailienant.json"
    rules_file.write_text(json.dumps({"rules": ["Always write tests", "Document your code"]}))

    result = rule_manager.get_combined_rules(str(tmp_path))

    assert "### Project Instructions:" in result
    assert "- Always write tests" in result
    assert "- Document your code" in result


def test_cache_skips_disk_on_unchanged_mtime(tmp_path, monkeypatch):
    """A second call without mtime change must NOT re-open the file."""
    rules_file = tmp_path / ".ailienant.json"
    rules_file.write_text(json.dumps({"rules": ["Rule A"]}))

    read_count = {"n": 0}
    _real_open = open

    def counting_open(path, *args, **kwargs):
        if str(path).endswith(".ailienant.json"):
            read_count["n"] += 1
        return _real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    rule_manager.get_combined_rules(str(tmp_path))  # cold read
    rule_manager.get_combined_rules(str(tmp_path))  # cache hit — no disk read

    assert read_count["n"] == 1, "File must be read only once when mtime is unchanged"


def test_rules_appear_in_planner_system_prompt(tmp_path):
    """Rules from .ailienant.json must be present in the assembled system prompt."""
    rules_file = tmp_path / ".ailienant.json"
    rules_file.write_text(json.dumps({"rules": ["Always write tests", "Keep functions small"]}))

    rules_str = rule_manager.get_combined_rules(str(tmp_path))
    system_prompt = build_safe_prompt(PLANNER_IDENTITY, context_str="", boundary="test_boundary")
    combined = system_prompt + "\n\n" + rules_str

    assert "Always write tests" in combined
    assert "Keep functions small" in combined
    assert "### Project Instructions:" in combined
