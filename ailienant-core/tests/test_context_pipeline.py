"""Division 8.12 checkpoint gate — Five-Layer Context Pipeline.

Locks the invariants that make the pipeline safe to wire into agent call paths:

  1. Layers 1-3 (Foundation/Project/Memory) are never silently evicted; the only
     budget failure is a hard ``ContextBudgetError`` when they alone exhaust the
     window.
  2. Layer 4 (Conversation) FIFO-evicts the oldest turns when the dynamic budget
     is exceeded, preserving order and the newest turns.
  3. The ``STATE_COMPACTED`` callback fires (and only fires) on real L4 eviction,
     with the wire-event shape the IDE consumes.
  4. Layer 5 (Execution) is token-exact tail-truncated — the assembled total never
     exceeds the budget even after the truncation marker is appended.
  5. The agent budget-guard (``build_agent_context``) trims Execution first and
     keeps the durable instruction context whole.

Test-only; sibling-gate convention. No network, no real connection manager — the
WS boundary is stubbed.
"""
from __future__ import annotations

import functools
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from brain.agent_context import (
    DEFAULT_CONTEXT_BUDGET,
    OUTPUT_BUDGET_MARGIN_TOKENS,
    build_agent_context,
    resolve_context_budget,
    resolve_output_budget,
    resolve_real_window,
)
from brain.context_pipeline import (
    ContextBudgetError,
    ContextChunk,
    ContextPipeline,
    FoundationLayer,
    ProjectLayer,
    _MARKER_TOKENS,
    _TRUNCATION_MARKER,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _chunk(brain: str, label: str, n_words: int) -> ContextChunk:
    """A measured chunk of roughly ``n_words`` distinct-word tokens."""
    return ContextChunk(body=" ".join([label] * n_words), brain=brain, label=label).measure()


# ── Invariant 1: L1-L3 never evicted ───────────────────────────────────

async def test_l1_l3_survive_overflow() -> None:
    pipe = ContextPipeline(total_token_budget=10_000)
    pipe.foundation.add(_chunk("foundation", "FND", 40))
    pipe.project.add(_chunk("project", "PRJ", 40))
    pipe.memory.add(_chunk("memory", "MEM", 40))
    for i in range(20):  # massive conversation overflow
        pipe.conversation.add(_chunk("conversation", f"CONV{i}", 200))

    res = await pipe.assemble()

    assert "FND" in res.content and "PRJ" in res.content and "MEM" in res.content
    assert res.l1_tokens > 0 and res.l2_tokens > 0 and res.l3_tokens > 0
    # Pinned layers untouched even though L4 was hammered.
    assert len(pipe.foundation.chunks()) == 1
    assert len(pipe.project.chunks()) == 1
    assert len(pipe.memory.chunks()) == 1
    assert res.l4_evicted > 0  # the pressure landed on L4, not L1-L3


async def test_budget_error_when_l1_l3_alone_exceed() -> None:
    pipe = ContextPipeline(total_token_budget=60)
    pipe.foundation.add(_chunk("foundation", "FND", 300))  # alone > effective budget
    with pytest.raises(ContextBudgetError):
        await pipe.assemble()


# ── Invariant 2: L4 FIFO eviction ──────────────────────────────────────

async def test_l4_fifo_drops_oldest_keeps_newest_in_order() -> None:
    pipe = ContextPipeline(total_token_budget=400)
    pipe.foundation.add(_chunk("foundation", "FND", 8))
    for i in range(10):
        pipe.conversation.add(_chunk("conversation", f"C{i}", 50))

    res = await pipe.assemble()

    assert res.l4_evicted > 0
    survivors = [c.label for c in pipe.conversation.chunks()]
    assert survivors, "partial eviction expected, not a full wipe"
    # Newest retained, oldest dropped.
    assert "C9" in survivors
    assert "C0" not in survivors
    # Order preserved (ascending suffixes).
    suffixes = [int(lbl[1:]) for lbl in survivors]
    assert suffixes == sorted(suffixes)


# ── Invariant 3: STATE_COMPACTED callback ──────────────────────────────

async def test_on_compacted_fires_once_on_eviction() -> None:
    seen: List[Tuple[str, int]] = []

    async def spy(message: str, turns: int) -> None:
        seen.append((message, turns))

    pipe = ContextPipeline(total_token_budget=400, on_compacted=spy)
    pipe.foundation.add(_chunk("foundation", "FND", 8))
    for i in range(10):
        pipe.conversation.add(_chunk("conversation", f"C{i}", 50))

    res = await pipe.assemble()

    assert len(seen) == 1
    message, turns = seen[0]
    assert turns == res.l4_evicted > 0
    assert "Compacted" in message


async def test_on_compacted_silent_without_eviction() -> None:
    seen: List[Tuple[str, int]] = []

    async def spy(message: str, turns: int) -> None:
        seen.append((message, turns))

    pipe = ContextPipeline(total_token_budget=10_000, on_compacted=spy)
    pipe.foundation.add(_chunk("foundation", "FND", 8))
    pipe.conversation.add(_chunk("conversation", "C", 8))

    await pipe.assemble()
    assert seen == []


async def test_broadcast_state_compacted_event_shape() -> None:
    """The producer builds the additive wire event the IDE consumes (hermetic stub)."""
    from api.ws_contracts import ServerStateCompactedEvent
    from api.websocket_manager import ConnectionManager

    sent: List[Tuple[str, object]] = []

    mgr = ConnectionManager()

    async def _spy(client_id: str, event: object) -> None:
        sent.append((client_id, event))

    mgr.send_personal_message = _spy  # type: ignore[method-assign]
    await mgr.broadcast_state_compacted("sess-1", "Compacted 3 conversation turn(s).", 3)

    assert len(sent) == 1
    sid, ev = sent[0]
    assert sid == "sess-1"
    assert isinstance(ev, ServerStateCompactedEvent)
    assert ev.event_type == "state_compacted"
    assert ev.data.session_id == "sess-1"
    assert ev.data.compaction_message == "Compacted 3 conversation turn(s)."
    assert ev.data.turns_compressed == 3
    # summary_text is additive/optional — absent by default, tolerated by consumers.
    assert ev.data.summary_text is None

    # When the summarizer produced prose, it rides in the same event.
    await mgr.broadcast_state_compacted("sess-1", "Compacted 3 turn(s).", 3, "The prose.")
    ev2 = sent[1][1]
    assert isinstance(ev2, ServerStateCompactedEvent)
    assert ev2.data.summary_text == "The prose."


async def test_on_compacted_partial_binding_arity() -> None:
    """`functools.partial(mgr.broadcast_state_compacted, session_id)` yields the
    (compaction_message, turns_compressed) arity the pipeline callback expects."""
    captured: dict[str, object] = {}

    async def broadcast(session_id: str, compaction_message: str, turns_compressed: int) -> None:
        captured.update(sid=session_id, msg=compaction_message, n=turns_compressed)

    cb = functools.partial(broadcast, "sess-9")
    await cb("hello", 2)  # the exact call shape ContextPipeline makes
    assert captured == {"sid": "sess-9", "msg": "hello", "n": 2}


# ── Invariant 4: L5 token-exact tail-truncation ────────────────────────

async def test_l5_tail_truncated_within_budget() -> None:
    budget = 300
    pipe = ContextPipeline(total_token_budget=budget)
    pipe.foundation.add(_chunk("foundation", "FND", 8))
    pipe.execution.add(_chunk("execution", "EXE", 500))  # far over

    res = await pipe.assemble()

    assert res.l5_truncated is True
    assert _TRUNCATION_MARKER.strip() in res.content
    # The marker cost is pre-deducted, so the assembled total stays within budget.
    assert res.total_tokens <= budget


async def test_l5_omitted_when_budget_below_marker() -> None:
    # effective budget after the safety buffer leaves < marker tokens for L5.
    pipe = ContextPipeline(total_token_budget=60 + _MARKER_TOKENS)
    pipe.foundation.add(_chunk("foundation", "FND", 1))
    pipe.execution.add(_chunk("execution", "EXE", 300))
    res = await pipe.assemble()
    assert res.l5_truncated is True
    assert res.l5_tokens == 0  # dropped entirely rather than overflow


# ── Invariant 5: layer mechanics ───────────────────────────────────────

def test_add_auto_measures_unmeasured_chunk() -> None:
    layer = FoundationLayer()
    layer.add(ContextChunk(body="hello world tokens", brain="foundation", label="x"))
    assert layer.chunks()[0].tokens > 0


def test_non_evictable_layers_ignore_evict() -> None:
    f = FoundationLayer()
    f.add(_chunk("foundation", "A", 5))
    f.add(_chunk("foundation", "B", 5))
    assert f.evict_oldest(1) == []
    assert len(f.chunks()) == 2

    p = ProjectLayer()
    p.add(_chunk("project", "A", 5))
    assert p.evict_oldest(5) == []
    assert len(p.chunks()) == 1


# ── Agent budget-guard (build_agent_context) ───────────────────────────

async def test_build_agent_context_blocks_assembled() -> None:
    res = await build_agent_context(
        total_token_budget=10_000,
        foundation=["IDENTITY-FND", "RULES-FND"],
        project=["PROJ-INSTR"],
        memory=["MEM-TRAJ"],
        execution=["BIG-FILE " * 5],
    )
    for token in ("IDENTITY-FND", "RULES-FND", "PROJ-INSTR", "MEM-TRAJ"):
        assert token in res.foundation_block
    assert "BIG-FILE" in res.execution_block


async def test_build_agent_context_drops_empty_sources() -> None:
    res = await build_agent_context(
        total_token_budget=10_000,
        foundation=["ONLY"],
        project=[""],     # empty → dropped, no stray separator
        memory=[],
    )
    assert res.foundation_block == "ONLY"


async def test_build_agent_context_truncates_execution_first() -> None:
    res = await build_agent_context(
        total_token_budget=200,
        foundation=["KEEP " * 5],
        execution=["DROP " * 500],
    )
    assert "KEEP" in res.foundation_block          # durable context whole
    assert res.assembly.l5_truncated is True       # volatile content trimmed


async def test_build_agent_context_raises_when_foundation_alone_overflows() -> None:
    with pytest.raises(ContextBudgetError):
        await build_agent_context(total_token_budget=60, foundation=["HUGE " * 300])


def test_resolve_context_budget_profile_and_fallback() -> None:
    assert resolve_context_budget({}) == DEFAULT_CONTEXT_BUDGET

    class _Profile:
        context_window = 32_000

    assert resolve_context_budget({"active_llm_profile": _Profile()}) == 32_000

    class _ZeroProfile:
        context_window = 0

    assert resolve_context_budget({"active_llm_profile": _ZeroProfile()}) == DEFAULT_CONTEXT_BUDGET


# ─────────────────────────────────────────────────────────────────────────
# resolve_output_budget — the joint input+output arithmetic every structured
# caller needs. Regression coverage for N8: the historical
# `min(declared_ceiling, budget // 2)` shape collapsed onto its own floor at
# the only budget the system ever actually resolved (8192 // 2 = 4096), so
# every caller received exactly 4096 no matter what it asked for. These pin
# that the REPLACEMENT arithmetic genuinely varies with the real prompt and
# the real window, not a declared constant.
# ─────────────────────────────────────────────────────────────────────────


def test_resolve_output_budget_grants_the_declared_ceiling_when_room_is_ample() -> None:
    decision = resolve_output_budget(
        prompt_tokens=3000, real_window=131_072, declared_ceiling=16_384,
    )
    assert decision.ok is True
    assert decision.max_tokens == 16_384


def test_resolve_output_budget_shrinks_to_the_real_remaining_room() -> None:
    """This is the exact shape of the incident this task investigated: a real
    prompt of ~3000 tokens against a real 4096-token window leaves only ~840
    tokens of usable output — nowhere near the 4096 every caller used to get
    regardless of the real window."""
    decision = resolve_output_budget(
        prompt_tokens=3000, real_window=4096, declared_ceiling=16_384,
    )
    assert decision.ok is True
    assert decision.max_tokens == 4096 - 3000 - OUTPUT_BUDGET_MARGIN_TOKENS
    assert decision.max_tokens < 4096  # strictly less than the old flat result


def test_resolve_output_budget_scales_with_the_real_prompt_not_a_constant() -> None:
    """N8: two different prompt sizes against the same window must yield two
    different ceilings — the historical bug returned the identical 4096 for
    every input because the input size never entered the calculation."""
    short = resolve_output_budget(prompt_tokens=500, real_window=8192, declared_ceiling=16_384)
    long = resolve_output_budget(prompt_tokens=5000, real_window=8192, declared_ceiling=16_384)
    assert short.max_tokens != long.max_tokens
    assert short.max_tokens > long.max_tokens


def test_resolve_output_budget_refuses_before_the_call_when_room_is_insufficient() -> None:
    """The pre-flight refusal (§4.c/OQ-1): when the prompt alone leaves no
    usable room, this must refuse honestly rather than hand back a tiny
    max_tokens that would just truncate silently downstream."""
    decision = resolve_output_budget(
        prompt_tokens=4000, real_window=4096, declared_ceiling=16_384,
    )
    assert decision.ok is False
    assert decision.max_tokens == 0
    assert decision.reason is not None
    assert "4096" in decision.reason  # names the real window, not a guess


def test_resolve_output_budget_honors_reserved_tokens() -> None:
    baseline = resolve_output_budget(prompt_tokens=1000, real_window=8192, declared_ceiling=16_384)
    reserved = resolve_output_budget(
        prompt_tokens=1000, real_window=8192, declared_ceiling=16_384, reserved_tokens=2000,
    )
    assert reserved.max_tokens == baseline.max_tokens - 2000


# ─────────────────────────────────────────────────────────────────────────
# resolve_real_window — probes the runtime via model_resolver, falling back
# to resolve_context_budget only when the tier can't be resolved or the probe
# returns unknown.
# ─────────────────────────────────────────────────────────────────────────


async def test_resolve_real_window_uses_the_probed_context_length() -> None:
    from core.config.model_resolver import RuntimeCapabilities

    fake_target = object()
    with patch("core.config.model_resolver.get_chat_target", return_value=fake_target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ):
        window = await resolve_real_window({}, tier="big")
    assert window == 131_072


async def test_resolve_real_window_falls_back_when_probe_is_unknown() -> None:
    from core.config.model_resolver import RuntimeCapabilities

    fake_target = object()
    with patch("core.config.model_resolver.get_chat_target", return_value=fake_target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
         ):
        window = await resolve_real_window({}, tier="big")
    assert window == DEFAULT_CONTEXT_BUDGET


async def test_resolve_real_window_uses_the_declared_window_for_a_remote_target() -> None:
    """The probe is Ollama-only, so a cloud model always came back unknown and was
    budgeted against the flat default — sizing a 1M-token model at 8192, which
    truncated structured output mid-object and then read as a schema error."""
    from core.config.model_resolver import RuntimeCapabilities

    remote = SimpleNamespace(model="gemini/gemini-2.5-flash", is_local=False)
    with patch("core.config.model_resolver.get_chat_target", return_value=remote), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
         ), \
         patch("core.config.model_resolver.resolve_declared_window", return_value=1_048_576):
        window = await resolve_real_window({}, tier="cloud")
    assert window == 1_048_576


async def test_resolve_real_window_ignores_the_declared_window_for_a_local_target() -> None:
    """A local model's architectural maximum overstates what this machine can
    serve, so the declared number must never substitute for the RAM-aware probe —
    an unknown probe on a local target keeps the conservative default."""
    from core.config.model_resolver import RuntimeCapabilities

    local = SimpleNamespace(model="ollama_chat/gemma4:e4b", is_local=True)
    with patch("core.config.model_resolver.get_chat_target", return_value=local), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
         ), \
         patch("core.config.model_resolver.resolve_declared_window", return_value=131_072) as declared:
        window = await resolve_real_window({}, tier="medium")
    assert window == DEFAULT_CONTEXT_BUDGET
    declared.assert_not_called()


async def test_resolve_real_window_falls_back_when_the_declared_window_is_unknown() -> None:
    from core.config.model_resolver import RuntimeCapabilities

    remote = SimpleNamespace(model="not-a-real/model-xyz", is_local=False)
    with patch("core.config.model_resolver.get_chat_target", return_value=remote), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=None, supports_thinking=False)),
         ), \
         patch("core.config.model_resolver.resolve_declared_window", return_value=None):
        window = await resolve_real_window({}, tier="cloud")
    assert window == DEFAULT_CONTEXT_BUDGET


async def test_resolve_real_window_falls_back_when_no_target_resolves() -> None:
    with patch("core.config.model_resolver.get_chat_target", return_value=None):
        window = await resolve_real_window({}, tier="big")
    assert window == DEFAULT_CONTEXT_BUDGET


async def test_resolve_real_window_falls_back_on_resolver_import_or_probe_error() -> None:
    """A probe fault must degrade to the existing conservative behaviour, never
    raise out of a caller that is mid-way through drafting a plan."""
    with patch("core.config.model_resolver.get_chat_target", side_effect=RuntimeError("boom")):
        window = await resolve_real_window({}, tier="big")
    assert window == DEFAULT_CONTEXT_BUDGET


async def test_resolve_real_window_prefers_the_ram_clamped_window_for_a_local_target() -> None:
    """The whole point of this fix: a local target's architectural ceiling
    (131_072) is routinely far larger than what THIS machine can actually
    serve. `resolve_num_ctx` (RAM-aware) must win over the raw architectural
    number whenever the resolved target is local — proven here by making the
    two disagree and asserting the SMALLER, RAM-aware value is returned."""
    from core.config.model_resolver import RuntimeCapabilities

    fake_local_target = SimpleNamespace(is_local=True)
    with patch("core.config.model_resolver.get_chat_target", return_value=fake_local_target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch(
             "core.config.model_resolver.resolve_num_ctx",
             new=AsyncMock(return_value=9_216),  # the RAM-clamped ceiling, deliberately << 131_072
         ):
        window = await resolve_real_window({}, tier="big")
    assert window == 9_216


async def test_resolve_real_window_keeps_the_architectural_ceiling_for_a_cloud_target() -> None:
    """A cloud target has no local RAM constraint — resolve_num_ctx must never
    even be consulted, let alone override the architectural number."""
    from core.config.model_resolver import RuntimeCapabilities

    fake_cloud_target = SimpleNamespace(is_local=False)
    with patch("core.config.model_resolver.get_chat_target", return_value=fake_cloud_target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=200_000, supports_thinking=False)),
         ), \
         patch("core.config.model_resolver.resolve_num_ctx", new=AsyncMock()) as mock_num_ctx:
        window = await resolve_real_window({}, tier="big")
    assert window == 200_000
    mock_num_ctx.assert_not_called()


async def test_resolve_real_window_falls_back_to_architectural_when_num_ctx_is_unresolvable() -> None:
    """If resolve_num_ctx itself can't resolve (non-Ollama, or an internal
    probe miss), degrade to the architectural ceiling rather than losing the
    window entirely."""
    from core.config.model_resolver import RuntimeCapabilities

    fake_local_target = SimpleNamespace(is_local=True)
    with patch("core.config.model_resolver.get_chat_target", return_value=fake_local_target), \
         patch(
             "core.config.model_resolver.probe_runtime_capabilities",
             new=AsyncMock(return_value=RuntimeCapabilities(context_length=131_072, supports_thinking=True)),
         ), \
         patch("core.config.model_resolver.resolve_num_ctx", new=AsyncMock(return_value=None)):
        window = await resolve_real_window({}, tier="big")
    assert window == 131_072
