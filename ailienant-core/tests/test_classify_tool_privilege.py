# ailienant-core/tests/test_classify_tool_privilege.py
#
# Unit tests for classify_tool_privilege — the fail-closed privilege tier
# resolver for tools discovered from untrusted external MCP servers.
#
# Every assertion checks an EXACT tier (never "!= DANGEROUS") so a test cannot
# pass for the wrong reason by falling through to the fail-closed default.
#
# DoD: pytest ailienant-core/tests/test_classify_tool_privilege.py -v passes.

from typing import Optional

import pytest

from core import permissions
from core.permissions import ToolPrivilegeTier, classify_tool_privilege

R = ToolPrivilegeTier.READ_ONLY
W = ToolPrivilegeTier.WRITE
E = ToolPrivilegeTier.EXECUTE
D = ToolPrivilegeTier.DANGEROUS


# ---------------------------------------------------------------------------
# Verb heuristic — one representative per tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("get_user", R),
        ("list_repos", R),
        ("read_file", R),
        ("create_pr", W),
        ("update_record", W),
        ("run_job", E),
        ("execute_command", E),
        ("execute_query", E),
        ("invoke_lambda", E),
        ("delete_repo", D),
        ("drop_table", D),
    ],
)
def test_verb_heuristic_by_name(name: str, expected: ToolPrivilegeTier) -> None:
    assert classify_tool_privilege(name) is expected


def test_unmatched_name_is_fail_closed() -> None:
    # No recognized verb in the name and no description -> DANGEROUS, never READ_ONLY.
    assert classify_tool_privilege("frobnicate") is D


def test_multi_tier_name_most_severe_wins() -> None:
    # "delete" (DANGEROUS) must beat the READ_ONLY token "get" in the same name.
    # An ascending-order scan would surface READ_ONLY and fail here.
    assert classify_tool_privilege("delete_and_get") is D


# ---------------------------------------------------------------------------
# Tokenizer — camelCase split is the mechanism, not the fallback
# ---------------------------------------------------------------------------


def test_camelcase_split_extracts_create() -> None:
    # WRITE is NOT the fail-closed default: if the tokenizer failed to split
    # camelCase, "createpullrequest" matches nothing and yields DANGEROUS.
    # Getting WRITE proves the "create" token was actually extracted.
    assert classify_tool_privilege("createPullRequest") is W


def test_camelcase_split_extracts_merge() -> None:
    assert classify_tool_privilege("mergePullRequest") is D


@pytest.mark.parametrize("name", ["asset_value", "charset", "subset_data"])
def test_no_substring_false_positive(name: str) -> None:
    # "set" must match only as a whole token, never as a substring of "asset"
    # / "charset" / "subset". None of these carry a real verb -> DANGEROUS.
    assert classify_tool_privilege(name) is D


def test_reset_is_whole_token_dangerous() -> None:
    assert classify_tool_privilege("reset_db") is D


def test_classification_is_case_insensitive() -> None:
    assert classify_tool_privilege("DELETE_THING") is D


# ---------------------------------------------------------------------------
# Description — elevates only, never downgrades
# ---------------------------------------------------------------------------


def test_description_only_supplies_signal() -> None:
    # Name has no verb; the "get" token in the description supplies the tier.
    assert classify_tool_privilege("ping_service", "get the service status") is R


def test_description_cannot_downgrade_name_tier() -> None:
    # "run" in the name pins EXECUTE; a read-flavored description must not
    # pull it down to READ_ONLY (the heuristic takes the max, never the min).
    assert classify_tool_privilege("run_everything", "read only, just fetches data") is E


# ---------------------------------------------------------------------------
# Curated catalog — authoritative, may downgrade as well as elevate
# ---------------------------------------------------------------------------


def test_catalog_overrides_and_can_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    # The catalog is trusted: a "delete_repo" the heuristic would call DANGEROUS
    # can be pinned READ_ONLY. Proves the seam is load-bearing while empty.
    monkeypatch.setitem(permissions._PRIVILEGE_CATALOG, "delete_repo", R)
    assert classify_tool_privilege("delete_repo") is R


def test_catalog_qualified_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        permissions._PRIVILEGE_CATALOG, "github.merge_pull_request", W
    )
    assert (
        classify_tool_privilege("merge_pull_request", server_name="github") is W
    )


@pytest.mark.parametrize("server_name", [None, "unknown"])
def test_catalog_empty_by_default(server_name: Optional[str]) -> None:
    # With no catalog entry, classification falls through to the heuristic.
    assert classify_tool_privilege("delete_repo", server_name=server_name) is D
