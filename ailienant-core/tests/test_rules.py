# ailienant-core/tests/test_rules.py
#
# Phase 2.24 + Phase 3.4.6: RuleManager with dual-rules resolver.
#
# Coverage:
#   1. Local .ailienant.json loaded & formatted (flat fallback)
#   2. Cache: same mtime -> no re-open
#   3. Integration: rules in planner system prompt
#   4. Global-only rules read from ~/.ailienant/.ailienant.json
#   5. Local subdir (.ailienant/.ailienant.json) preferred over flat
#   6. Local list rules override global with dedupe (DoD anchor)
#   7. Local dict rules deep-merge global
#   8. Cache invalidates when either file's mtime changes
#   9. No files -> default rules string

import json
from pathlib import Path

import pytest

from core.rules import rule_manager
from prompts import build_safe_prompt
from shared.rbac import PLANNER_IDENTITY


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Sandbox Path.home() to a tmp dir so the dev's real ~/.ailienant doesn't leak.

    Also resets the RuleManager singleton cache between tests.
    """
    fake_home = tmp_path / "_home_"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    rule_manager.reset()
    yield fake_home
    rule_manager.reset()


# ---------- legacy coverage (preserved) ----------

def test_loads_local_rules_file(tmp_path, _isolate_home):
    rules_file = tmp_path / ".ailienant.json"
    rules_file.write_text(json.dumps({"rules": ["Always write tests", "Document your code"]}))

    result = rule_manager.get_combined_rules(str(tmp_path))

    assert "### Project Instructions:" in result
    assert "- Always write tests" in result
    assert "- Document your code" in result


def test_cache_skips_disk_on_unchanged_mtime(tmp_path, _isolate_home, monkeypatch):
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
    rule_manager.get_combined_rules(str(tmp_path))  # cache hit

    assert read_count["n"] == 1, "File must be opened only once when mtime is unchanged"


def test_rules_appear_in_planner_system_prompt(tmp_path, _isolate_home):
    rules_file = tmp_path / ".ailienant.json"
    rules_file.write_text(json.dumps({"rules": ["Always write tests", "Keep functions small"]}))

    rules_str = rule_manager.get_combined_rules(str(tmp_path))
    system_prompt = build_safe_prompt(PLANNER_IDENTITY, context_str="", boundary="test_boundary")
    combined = system_prompt + "\n\n" + rules_str

    assert "Always write tests" in combined
    assert "Keep functions small" in combined
    assert "### Project Instructions:" in combined


# ---------- Phase 3.4.6: dual-scope resolution ----------

def test_global_only_rules(tmp_path, _isolate_home):
    """Rules from ~/.ailienant/.ailienant.json are read when no local file exists."""
    fake_home = _isolate_home
    global_dir = fake_home / ".ailienant"
    global_dir.mkdir()
    (global_dir / ".ailienant.json").write_text(json.dumps({"rules": ["GlobalRule"]}))

    result = rule_manager.get_combined_rules(str(tmp_path))

    assert "- GlobalRule" in result


def test_local_subdir_preferred_over_flat(tmp_path, _isolate_home):
    """When both <ws>/.ailienant.json AND <ws>/.ailienant/.ailienant.json exist, subdir wins."""
    (tmp_path / ".ailienant.json").write_text(json.dumps({"rules": ["FLAT"]}))
    sub = tmp_path / ".ailienant"
    sub.mkdir()
    (sub / ".ailienant.json").write_text(json.dumps({"rules": ["SUB"]}))

    result = rule_manager.get_combined_rules(str(tmp_path))

    assert "- SUB" in result
    assert "- FLAT" not in result


def test_local_list_overrides_global_with_dedupe(tmp_path, _isolate_home):
    """DoD #2 anchor: local rules appear first; duplicates removed; global appended."""
    fake_home = _isolate_home
    (fake_home / ".ailienant").mkdir()
    (fake_home / ".ailienant" / ".ailienant.json").write_text(json.dumps({"rules": ["A", "B"]}))

    (tmp_path / ".ailienant.json").write_text(json.dumps({"rules": ["B", "C"]}))

    result = rule_manager.get_combined_rules(str(tmp_path))

    # All three present.
    assert "- A" in result
    assert "- B" in result
    assert "- C" in result
    # Local list (["B","C"]) is preserved verbatim; then global's unseen entries
    # appended in order ("A"). Duplicate "B" appears only once.
    # Expected order: B, C, A.
    pos_b = result.find("- B")
    pos_c = result.find("- C")
    pos_a = result.find("- A")
    assert pos_b < pos_c < pos_a, (
        f"Expected local-first order B<C<A (local=[B,C], then non-dup global A), "
        f"got positions B={pos_b} C={pos_c} A={pos_a}"
    )
    # Dedupe: only ONE "- B" line.
    assert result.count("- B") == 1


def test_local_dict_deep_merges_global(tmp_path, _isolate_home):
    """Dict-shape rules: local overrides global per key; nested lists concat+dedupe."""
    fake_home = _isolate_home
    (fake_home / ".ailienant").mkdir()
    (fake_home / ".ailienant" / ".ailienant.json").write_text(json.dumps({
        "rules": {"deps": ["x"], "style": ["pep8"]},
    }))
    (tmp_path / ".ailienant.json").write_text(json.dumps({
        "rules": {"deps": ["y"]},
    }))

    result = rule_manager.get_combined_rules(str(tmp_path))

    # Both deps values present, local-first (concat+dedupe inside the nested list).
    assert 'deps: ["y","x"]' in result
    # Untouched global key preserved.
    assert 'style: ["pep8"]' in result


def test_cache_invalidates_when_global_file_changes(tmp_path, _isolate_home):
    """Changing the global file's mtime must trigger a re-read."""
    fake_home = _isolate_home
    global_dir = fake_home / ".ailienant"
    global_dir.mkdir()
    gfile = global_dir / ".ailienant.json"
    gfile.write_text(json.dumps({"rules": ["v1"]}))

    out1 = rule_manager.get_combined_rules(str(tmp_path))
    assert "- v1" in out1

    # Modify global file. Bump mtime explicitly because the change may land in
    # the same second on fast filesystems.
    gfile.write_text(json.dumps({"rules": ["v2"]}))
    import os
    new_mtime = os.path.getmtime(str(gfile)) + 5.0
    os.utime(str(gfile), (new_mtime, new_mtime))

    out2 = rule_manager.get_combined_rules(str(tmp_path))
    assert "- v2" in out2
    assert "- v1" not in out2


def test_no_files_returns_default(tmp_path, _isolate_home):
    """When neither global nor local file exists, return the default string."""
    result = rule_manager.get_combined_rules(str(tmp_path))
    assert "Follow standard best practices." in result


def test_empty_project_path_falls_back_to_global_only(tmp_path, _isolate_home):
    """Calling with project_path='' still consults the global file."""
    fake_home = _isolate_home
    (fake_home / ".ailienant").mkdir()
    (fake_home / ".ailienant" / ".ailienant.json").write_text(json.dumps({"rules": ["G-ONLY"]}))

    result = rule_manager.get_combined_rules("")

    assert "- G-ONLY" in result
