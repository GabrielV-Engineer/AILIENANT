# tests/test_analyst_agreement.py
"""DEBT-180 — `_is_agreement` must be anchored to the whole message, not an
unanchored substring search.

`agents/analyst.py::_is_agreement` gates the Socratic grill's fast-path
early-exit: a bare agreement reply ("yes", "looks good") ends the grill
immediately, skipping a redundant round. Before this fix it checked
`any(signal in text for signal in _AGREEMENT_SIGNALS)` against the whole
lowercased reply — so a signal appearing ANYWHERE, including as the opening
word of a much longer substantive answer, tripped a false match and cut the
conversation off mid-elaboration (the reported case: "Yes, establish
component files for Header, HeroSlider..." reads as "yes").
"""
from __future__ import annotations

from agents.analyst import _is_agreement


# ── True positives: bare agreement, the fast path should fire ───────────────


def test_bare_single_word_agreement() -> None:
    assert _is_agreement("yes") is True
    assert _is_agreement("Yes") is True
    assert _is_agreement("OK") is True
    assert _is_agreement("dale") is True


def test_bare_multiword_agreement() -> None:
    assert _is_agreement("looks good") is True
    assert _is_agreement("Looks Good") is True
    assert _is_agreement("de acuerdo") is True


def test_agreement_survives_surrounding_whitespace_and_punctuation() -> None:
    assert _is_agreement("  yes.  ") is True
    assert _is_agreement("dale!") is True
    assert _is_agreement("¿bien?") is True
    assert _is_agreement("LGTM!") is True


def test_compound_agreement_of_two_signals_still_matches() -> None:
    """The frontend's own canonical plan-acceptance phrase
    (`AGREEMENT_SIGNAL` in Workspace.tsx) is two signals joined by a comma,
    not a single literal entry in `_AGREEMENT_SIGNALS` — every clause must
    independently be a signal for the compound to count."""
    assert _is_agreement("Looks good, proceed.") is True
    assert _is_agreement("Perfect. Ship it.") is True


def test_compound_agreement_with_a_leading_filler_connective_still_matches() -> None:
    """Pre-existing regression coverage (test_ideation.py,
    test_ideation_handoff_contract.py) pins "looks good, let's proceed" as
    agreement — "let's proceed" is a filler ("let's ") plus the signal
    "proceed", not a literal entry in `_AGREEMENT_SIGNALS` on its own."""
    assert _is_agreement("looks good, let's proceed") is True
    assert _is_agreement("let's proceed") is True
    assert _is_agreement("please proceed") is True


# ── True negatives: substantive answers that merely open with a signal word ──


def test_substantive_answer_opening_with_yes_is_not_agreement() -> None:
    """The exact false-positive the entry reports."""
    text = "Yes, establish component files for Header, HeroSlider, Footer and wire them up."
    assert _is_agreement(text) is False


def test_substantive_answer_opening_with_ok_is_not_agreement() -> None:
    assert _is_agreement("Ok, but let's also add dark mode and a settings page.") is False


def test_original_task_brief_containing_a_signal_word_is_not_agreement() -> None:
    """Mirrors the round_count==0 guard's own worked example in analyst.py:
    a first-turn task description must never be misread as agreement just
    because it contains a short signal token."""
    assert _is_agreement("Yes, add a dark mode toggle to the settings panel.") is False


def test_empty_or_whitespace_only_is_not_agreement() -> None:
    assert _is_agreement("") is False
    assert _is_agreement("   ") is False
