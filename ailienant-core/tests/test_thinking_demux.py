"""Unit tests for the scaffolded-reasoning stream splitter.

Exercises every state transition and the answer-mission-critical degrade rules:
clean block, tag split across chunk boundaries, ignored scaffold, unclosed tag
(no leak), and discarded preamble.
"""

from __future__ import annotations

from typing import Iterable

from tools.thinking_demux import ThinkingTagDemuxer


def _run(chunks: Iterable[str]) -> tuple[str, str]:
    """Feed all chunks, then finish; return the joined (reasoning, answer)."""
    demux = ThinkingTagDemuxer()
    think: list[str] = []
    answer: list[str] = []
    for c in chunks:
        t, a = demux.feed(c)
        think.append(t)
        answer.append(a)
    t, a = demux.finish()
    think.append(t)
    answer.append(a)
    return "".join(think), "".join(answer)


def test_clean_single_chunk_block() -> None:
    think, answer = _run(["<thinking>I reason here</thinking>the answer"])
    assert think == "I reason here"
    assert answer == "the answer"


def test_tag_split_across_chunks() -> None:
    # Both the open and the close tag straddle chunk boundaries.
    think, answer = _run(["<thin", "king>rea", "son</thin", "king>ans", "wer"])
    assert think == "reason"
    assert answer == "answer"


def test_close_tag_split_across_chunks() -> None:
    think, answer = _run(["<thinking>abc</th", "inking>xyz"])
    assert think == "abc"
    assert answer == "xyz"


def test_no_tag_is_treated_as_answer() -> None:
    # Model ignored the scaffold entirely — the whole stream is the answer.
    think, answer = _run(["just ", "the ", "answer"])
    assert think == ""
    assert answer == "just the answer"


def test_unclosed_tag_yields_empty_answer_no_leak() -> None:
    # Opened but never closed: reasoning is surfaced, the answer stays empty so
    # reasoning never leaks into it.
    think, answer = _run(["<thinking>reasoning ", "continues forever"])
    assert think == "reasoning continues forever"
    assert answer == ""


def test_preamble_before_open_is_discarded() -> None:
    think, answer = _run(["Sure! <thinking>reason</thinking>done"])
    assert think == "reason"
    assert answer == "done"


def test_tags_never_appear_in_either_channel() -> None:
    think, answer = _run(["x<thinking>a</thinking>b"])
    assert "<thinking>" not in think and "</thinking>" not in think
    assert "<thinking>" not in answer and "</thinking>" not in answer


def test_multiline_reasoning_preserved() -> None:
    body = "step one\n\nstep two\n- bullet"
    think, answer = _run([f"<thinking>{body}</thinking>ok"])
    assert think == body
    assert answer == "ok"


def test_empty_thinking_block() -> None:
    think, answer = _run(["<thinking></thinking>answer only"])
    assert think == ""
    assert answer == "answer only"


def test_incremental_returns_stream_reasoning_live() -> None:
    # The reasoning must surface progressively (live), not only at finish.
    demux = ThinkingTagDemuxer()
    t1, _ = demux.feed("<thinking>first ")
    t2, _ = demux.feed("second")
    assert t1 == "first "
    assert t2 == "second"
