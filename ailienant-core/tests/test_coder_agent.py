# tests/test_coder_agent.py
"""Phase 4.1.4 DoD — CoderAgent Cognitive Policy Engine + 8-role schema widening.

Four tests cover:
  A. Tool whitelist resolution (doc_manager — no BashTool).
  B. HITL flag emission when devops_infra touches .env.
  C. Ephemeral system prompt does NOT leak to state.messages OR appear as a
     non-state key in the result dict (R1 — LangGraph state-merge contract).
  D. Legacy 5-value target_role migrates to new 8-value canonical name
     end-to-end through the Coder.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.state import MissionSpecification, WBSStep


def _fake_llm_response(content: str) -> Any:
    """Minimal stand-in for a litellm ModelResponse (resp.choices[0].message.content)."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_step(
    n: int = 1,
    role: str = "core_dev",
    action: str = "edit_file",
    target_file: str = "main.py",
    description: str = "Stub step.",
    status: str = "pending",
) -> WBSStep:
    return WBSStep(
        step_number=n,
        target_role=role,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        target_file=target_file,
        description=description,
        status=status,  # type: ignore[arg-type]
    )


def _make_mission(tasks: List[WBSStep]) -> MissionSpecification:
    return MissionSpecification(
        outcome="Test outcome.",
        scope=["main.py"],
        constraints=["No external deps."],
        decisions=["Use the test runner."],
        tasks=tasks,
        checks=["Pytest exits 0."],
    )


def _make_state(mission: MissionSpecification, step_id: int = 1, **overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "coder-test",
        "mission_spec": mission,
        "current_step_id": step_id,
        "retry_count": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _mock_coder_io() -> Any:
    """Isolate run_coder_node from I/O: WS broadcast, RAG, VFS read, and the LLM.

    Defaults: LLM returns an empty edit set, the file reads as simple Python, RAG is
    empty. Individual tests can nest their own patches to override these.
    """
    from core.vfs_middleware import VFSReadResult
    with patch(
        "api.websocket_manager.vfs_manager.emit_graph_mutation",
        new=AsyncMock(return_value=None),
    ), patch(
        "core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content="def foo():\n    return 1\n"),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response("")),
    ):
        yield


# ── Test A — doc_manager tool whitelist ──────────────────────────────────────


def test_coder_agent_resolves_doc_manager_tool_whitelist() -> None:
    """doc_manager must NOT have BashTool; must have WriteFileTool + apply_patch."""
    from agents.roles import ROLE_REGISTRY

    whitelist = ROLE_REGISTRY["doc_manager"]["allowed_tools"]
    assert "BashTool" not in whitelist
    assert "pytest" not in whitelist
    assert "WriteFileTool" in whitelist
    assert "apply_patch" in whitelist
    assert "FileReadTool" in whitelist


# ── Test B — devops_infra HITL flag on .env ──────────────────────────────────


@pytest.mark.anyio
async def test_coder_agent_emits_hitl_flag_when_devops_touches_dotenv() -> None:
    step = _make_step(
        role="devops_infra",
        action="write_file",
        target_file=".env",
        description="Update DATABASE_URL secret.",
    )
    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node

    result = await run_coder_node(state)

    assert "security_flags" in result
    flags: List[str] = result["security_flags"]
    matches = [f for f in flags if f.startswith("HITL_APPROVAL_REQUIRED:devops_infra:.env")]
    assert matches, f"Expected HITL flag for .env trigger, got: {flags}"


# ── Test C — ephemeral system prompt does NOT leak (R1: state-key contract) ──


@pytest.mark.anyio
async def test_coder_agent_ephemeral_system_prompt_does_not_leak_to_messages_or_state() -> None:
    step = _make_step(role="secops")
    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node
    from agents.roles import build_coder_system_prompt

    result = await run_coder_node(state)

    # CRITICAL: the result dict must NOT contain any non-state key — LangGraph
    # would otherwise break state-merge or bloat the SQLite checkpoint.
    assert "messages" not in result
    assert "allowed_tools" not in result
    assert "ephemeral_system_prompt" not in result
    assert "role_config" not in result

    # Every returned key must be a declared field on AIlienantGraphState.
    allowed_state_keys = {
        "vfs_buffer",
        "pending_patches",    # coder proposes diffs, never writes directly
        "pending_contents",   # full new content for the write pipeline
        "pending_base_hash",  # pre-edit hash for the stale guard
        "pending_step_files", # paths this step touched, scoping the apply-gate commit
        "apply_feedback",     # cleared unconditionally so a revision never replays stale cache
        "mission_spec",       # durable WBS-step status delta (multi-step loop)
        "target_role",
        "current_step_id",
        "current_cost_usd",
        "security_flags",
        "errors",
    }
    assert set(result.keys()) <= allowed_state_keys, (
        f"Coder returned non-state keys: {set(result.keys()) - allowed_state_keys}"
    )

    # The builder still produces the SecOps directive — proves the prompt is
    # constructable for Phase 5's MCP executor, just never persisted to state.
    secops_prompt = build_coder_system_prompt("secops")
    assert "OWASP Top-10 enforced" in secops_prompt
    assert "secops" in secops_prompt


# ── Test D — legacy role migrates end-to-end through the Coder ────────────────


@pytest.mark.anyio
async def test_coder_agent_legacy_role_migrates_to_new_via_validator() -> None:
    # Construct with legacy "Test" → before-validator maps to "qa_tester".
    step = _make_step(role="Test", target_file="tests/foo.py")
    assert step.target_role == "qa_tester", (
        "WBSStep before-validator must migrate legacy 'Test' to canonical "
        f"'qa_tester' on construction, got: {step.target_role}"
    )

    state = _make_state(_make_mission([step]))

    from agents.coder import run_coder_node

    result = await run_coder_node(state)

    assert result["target_role"] == "qa_tester"


# ── Test E — a valid AtomicPatch edit produces a unified diff (Phase 7.9.B.16) ─


@pytest.mark.anyio
async def test_coder_produces_unified_diff_for_valid_edit() -> None:
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    content = "def calculate(x):\n    return x + 1\n"
    edit_blob = (
        "### EDIT calc.py\n"
        "<<<<<<< SEARCH\n"
        "    return x + 1\n"
        "=======\n"
        "    return x + 2\n"
        ">>>>>>> REPLACE\n"
    )
    step = _make_step(action="edit_file", target_file="calc.py", description="Bump increment.")
    state = _make_state(_make_mission([step]))

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response(edit_blob)),
    ):
        result = await run_coder_node(state)

    assert "pending_patches" in result
    assert "calc.py" in result["pending_patches"], result
    diff = result["pending_patches"]["calc.py"]
    assert "return x + 2" in diff and "return x + 1" in diff

    # Phase 7.9.B.18 — the coder also emits the full new content + a pre-edit hash.
    from agents.coder import content_hash
    assert result["pending_contents"]["calc.py"] == "def calculate(x):\n    return x + 2\n"
    assert result["pending_base_hash"]["calc.py"] == content_hash(content)


def test_content_hash_is_crlf_stable() -> None:
    """Python text-mode reads collapse CRLF->LF while VS Code's doc.getText() keeps
    the editor EOL — content_hash must treat both representations as identical, or
    every Windows (CRLF) file falsely reads as stale at apply time."""
    from agents.coder import content_hash

    lf = "line one\nline two\n"
    crlf = "line one\r\nline two\r\n"
    assert content_hash(lf) == content_hash(crlf)


def test_content_hash_is_bom_stable() -> None:
    """A leading UTF-8 BOM must not change the hash: open(path, encoding='utf-8')
    decodes a BOM as a literal U+FEFF character, while VS Code's TextDocument.getText()
    never includes it — without stripping the BOM here, any file saved with one (e.g.
    PowerShell's Out-File/Set-Content, which default to BOM-prefixed UTF-8) would
    permanently, deterministically read as stale even when nothing actually changed."""
    from agents.coder import content_hash

    plain = "def calculate(x):\n    return x + 1\n"
    with_bom = "\ufeff" + plain
    assert content_hash(plain) == content_hash(with_bom)
    # Only a genuinely different body still produces a different hash.
    assert content_hash(plain) != content_hash(plain + "extra\n")


# \u2500\u2500 Item C \u2014 complexity-scaled coder output ceiling \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


class TestResolveCoderMaxTokens:
    """`_resolve_coder_max_tokens` scales the SEARCH/REPLACE output ceiling by
    step complexity instead of the flat 4096 default that produced stub files
    on a write_file step for a whole new module (audited 2026-07-28 live-test
    sweep)."""

    def test_never_below_flat_default(self) -> None:
        from agents.coder import _resolve_coder_max_tokens, _CODER_MIN_MAX_TOKENS

        step = _make_step(action="edit_file", description="Tiny fix.")
        assert _resolve_coder_max_tokens(step, "x", budget=200_000) >= _CODER_MIN_MAX_TOKENS

    def test_new_file_scales_with_description_length(self) -> None:
        """A write_file step's ceiling grows with how much the task describes,
        since a new file IS the entire REPLACE-side output."""
        from agents.coder import _resolve_coder_max_tokens

        short_step = _make_step(action="write_file", description="A tiny script.")
        long_step = _make_step(action="write_file", description="Implement " * 500)
        short_ceiling = _resolve_coder_max_tokens(short_step, None, budget=200_000)
        long_ceiling = _resolve_coder_max_tokens(long_step, None, budget=200_000)
        assert long_ceiling > short_ceiling

    def test_edit_scales_with_existing_file_size(self) -> None:
        """An edit_file step's ceiling grows with the target file's own size \u2014
        more file to anchor SEARCH blocks against and reproduce."""
        from agents.coder import _resolve_coder_max_tokens

        step = _make_step(action="edit_file", description="Refactor it.")
        small_ceiling = _resolve_coder_max_tokens(step, "x" * 100, budget=200_000)
        large_ceiling = _resolve_coder_max_tokens(step, "x" * 50_000, budget=200_000)
        assert large_ceiling > small_ceiling

    def test_bounded_by_half_the_resolved_budget(self) -> None:
        """Never exceeds half the resolved model's real context window, even for
        a huge file/description \u2014 the prompt itself needs the other half."""
        from agents.coder import _resolve_coder_max_tokens

        step = _make_step(action="write_file", description="Implement " * 5000)
        ceiling = _resolve_coder_max_tokens(step, None, budget=200_000)
        assert ceiling <= 100_000

    def test_real_context_window_wins_over_the_flat_floor(self) -> None:
        """A genuinely tiny context window (small local model) must cap max_tokens
        at half its real budget even when that dips below the historical 4096
        floor \u2014 asking for more completion tokens than the window has room for
        is a real correctness bug, not a safe default."""
        from agents.coder import _resolve_coder_max_tokens

        step = _make_step(action="write_file", description="Implement " * 5000)
        ceiling = _resolve_coder_max_tokens(step, None, budget=2048)
        assert ceiling <= 1024

    def test_malformed_input_degrades_to_flat_default(self) -> None:
        """Any unexpected input (e.g. a step missing an attribute) never raises \u2014
        it degrades to the historical flat default."""
        from agents.coder import _resolve_coder_max_tokens, _CODER_MIN_MAX_TOKENS

        broken_step = cast(WBSStep, object())  # deliberately wrong shape — no .action/.description
        assert _resolve_coder_max_tokens(broken_step, "x", budget=200_000) == _CODER_MIN_MAX_TOKENS


# \u2500\u2500 Item B \u2014 cross-project RAG relevance filtering \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


@pytest.mark.anyio
async def test_fetch_rag_snippets_filters_unrelated_top_level_project() -> None:
    """A workspace root spanning two unrelated projects must not inject the
    other project's code into this one's context \u2014 see core/utils.py's
    filter_relevant_snippets, applied inside _fetch_rag_snippets."""
    from agents.coder import _fetch_rag_snippets

    async def fake_search(*args, **kwargs):
        return [
            ("GameData/player.py", "class Player: ..."),
            ("App_transcription/audio.py", "def transcribe(): ..."),
        ]

    result = await _fetch_rag_snippets(
        "GameData/score.py", "add scoring", "proj-1", retrieval_fn=fake_search,
    )
    assert result == [("GameData/player.py", "class Player: ...")]


@pytest.mark.anyio
async def test_fetch_rag_snippets_keeps_explicit_mentions() -> None:
    from agents.coder import _fetch_rag_snippets

    async def fake_search(*args, **kwargs):
        return [("App_transcription/audio.py", "def transcribe(): ...")]

    result = await _fetch_rag_snippets(
        "GameData/score.py", "add scoring", "proj-1", retrieval_fn=fake_search,
        explicit_mentions=["App_transcription/audio.py"],
    )
    assert result == [("App_transcription/audio.py", "def transcribe(): ...")]


# ── 11.12 — MissionSpecification.to_context_block() ───────────────────────────
#
# The stack decision the planner records in `decisions` was write-only across the
# whole backend: the planner filled it, PlanPanel/PlanAcceptancePanel rendered it,
# but no code generator ever read it back, so a correct choice evaporated before
# generation. This bounded projection is the propagation fix.


class TestMissionSpecificationToContextBlock:
    def test_empty_spec_returns_empty_string(self) -> None:
        mission = MissionSpecification(
            outcome="x", scope=[], constraints=[], decisions=[], tasks=[], checks=[],
        )
        assert mission.to_context_block() == ""

    def test_includes_decisions_and_constraints(self) -> None:
        mission = _make_mission([_make_step()])
        mission.constraints.append("Follow house style.")
        block = mission.to_context_block()
        assert "Use the test runner." in block
        assert "Follow house style." in block

    def test_decisions_ordered_before_constraints(self) -> None:
        """The stack decision is convention-first in `decisions` — it must survive
        truncation, which only holds if decisions are emitted first."""
        mission = _make_mission([_make_step()])
        mission.decisions = ["Stack: Godot — 2D game, GDScript is the native fit."]
        mission.constraints = ["No external dependencies."]
        block = mission.to_context_block()
        assert block.index("Stack: Godot") < block.index("No external dependencies")

    def test_respects_entry_cap(self) -> None:
        mission = _make_mission([_make_step()])
        mission.decisions = [f"Decision {i}" for i in range(20)]
        block = mission.to_context_block(max_entries=3)
        assert block.count("Decision ") == 3

    def test_respects_char_cap_and_truncates_rather_than_raises(self) -> None:
        mission = _make_mission([_make_step()])
        mission.decisions = ["Stack: " + ("x" * 5000)]
        block = mission.to_context_block(max_chars=100)
        assert len(block) < 200  # header + capped body, not the full 5000-char entry

    def test_malformed_spec_field_never_raises(self) -> None:
        """A defensively-typed caller (Any from state.get) must not crash the turn
        even if a field somehow ended up wrong-shaped after LLM coercion quirks."""
        mission = _make_mission([_make_step()])
        mission.decisions = []  # empty is a legitimate, common case — must not raise
        mission.constraints = []
        assert mission.to_context_block() == ""


# ── 11.12 — mission context reaches BOTH code generators ──────────────────────


@pytest.mark.anyio
async def test_coder_prompt_includes_mission_context_block() -> None:
    """agents/coder.py previously read mission_spec only for step lookup/status —
    the stack decision never reached the generation prompt. Assert it now does,
    and that it precedes the file content (mission context leads L5, so it is the
    last chunk trimmed under budget pressure)."""
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    mission = _make_mission([_make_step(target_file="calc.py")])
    mission.decisions = ["Stack: Godot — 2D game, GDScript is the native fit."]
    state = _make_state(mission, step_id=1)
    content = "def calculate(x):\n    return x + 1\n"
    edit_blob = (
        "### EDIT calc.py\n<<<<<<< SEARCH\n    return x + 1\n=======\n"
        "    return x + 2\n>>>>>>> REPLACE\n"
    )

    captured_messages: List[Any] = []

    async def _capture_ainvoke(*, messages: Any, **_kwargs: Any) -> Any:
        captured_messages.extend(messages)
        return _fake_llm_response(edit_blob)

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=_capture_ainvoke),
    ):
        await run_coder_node(state)

    user_content = next(m["content"] for m in captured_messages if m["role"] == "user")
    assert "Stack: Godot" in user_content
    assert user_content.index("Stack: Godot") < user_content.index(content.splitlines()[0])


# ── 13.0.9 — agent_notes reaches the prompt, description stays clean ─────────


@pytest.mark.anyio
async def test_agent_notes_reaches_the_prompt_but_never_touches_description() -> None:
    """agent_notes (e.g. the polyglot patch-tool directive, agents/planner.py) is
    a coder-only channel — this asserts it lands in the generation prompt while
    the human-facing description string it was split out of stays untouched."""
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    step = _make_step(target_file="App.vue", description="Add a prop.").model_copy(
        update={"agent_notes": "[!] POLYGLOT FILE DETECTED: App.vue. Use patch_file."}
    )
    mission = _make_mission([step])
    state = _make_state(mission, step_id=1)
    content = "<template></template>\n"
    edit_blob = (
        "### EDIT App.vue\n<<<<<<< SEARCH\n<template></template>\n=======\n"
        "<template><p/></template>\n>>>>>>> REPLACE\n"
    )

    captured_messages: List[Any] = []

    async def _capture_ainvoke(*, messages: Any, **_kwargs: Any) -> Any:
        captured_messages.extend(messages)
        return _fake_llm_response(edit_blob)

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=_capture_ainvoke),
    ):
        await run_coder_node(state)

    user_content = next(m["content"] for m in captured_messages if m["role"] == "user")
    assert "Use patch_file" in user_content
    # The description itself was never mutated with the directive text.
    assert step.description == "Add a prop."


# ── 13.0.9 — honest native-thinking: a separate pass on non-native models ────


def _fake_astream_reasoning_factory(reasoning_chunks: List[str]):
    """A fake ``astream_reasoning`` — the coder's new pre-generation reasoning
    pass is its only caller, so (unlike the planner's fake) there is no second
    call shape to branch on."""
    from tools.stream_delta import StreamDelta

    async def _fake(messages, tier="big", *, temperature=0.0, max_tokens=512,
                     timeout=60.0, session_id=None, thinking_budget_tokens=4096,
                     free_form_answer=False):
        for chunk in reasoning_chunks:
            yield StreamDelta("thinking", chunk, "simulated")

    def _factory(*args: Any, **kwargs: Any) -> Any:
        return _fake(*args, **kwargs)

    return _factory


@pytest.mark.anyio
async def test_coder_streams_a_reasoning_pass_before_generation_on_nonnative_model() -> None:
    """The toggle previously did nothing at all for a local/unsupported model
    during a coding turn — assert the new pass actually reaches the sink."""
    collected: List[tuple[str, str]] = []

    async def _sink(text: str, source: str) -> None:
        collected.append((text, source))

    mission = _make_mission([_make_step()])
    state = _make_state(mission, step_id=1)
    cfg: Dict[str, Any] = {
        "configurable": {
            "stream_thinking": _sink,
            "enable_native_thinking": True,
            "thinking_budget_tokens": 4096,
        }
    }
    fake = _fake_astream_reasoning_factory(["Looking at the file, ", "I'll adjust the return."])

    with patch("tools.llm_gateway.LLMGateway.astream_reasoning", fake), patch(
        "core.config.model_resolver.get_chat_target",
        return_value=MagicMock(model="ollama/llama3"),
    ), patch("tools.llm_gateway._supports_native_thinking", return_value=False):
        from agents.coder import run_coder_node

        await run_coder_node(state, cast(Any, cfg))

    reasoning_deltas = [t for t, _ in collected]
    assert len(reasoning_deltas) == 2
    assert "".join(reasoning_deltas).startswith("Looking at the file")


@pytest.mark.anyio
async def test_coder_skips_the_reasoning_pass_on_a_native_model() -> None:
    """A native model already streams its own reasoning inside
    acomplete_with_thinking — the extra pass must not fire (no double trace)."""
    collected: List[tuple[str, str]] = []

    async def _sink(text: str, source: str) -> None:
        collected.append((text, source))

    mission = _make_mission([_make_step()])
    state = _make_state(mission, step_id=1)
    cfg: Dict[str, Any] = {
        "configurable": {
            "stream_thinking": _sink,
            "enable_native_thinking": True,
            "thinking_budget_tokens": 4096,
        }
    }
    fake = _fake_astream_reasoning_factory(["should NOT be emitted"])

    with patch("tools.llm_gateway.LLMGateway.astream_reasoning", fake), patch(
        "core.config.model_resolver.get_chat_target",
        return_value=MagicMock(model="claude-sonnet-5"),
    ), patch("tools.llm_gateway._supports_native_thinking", return_value=True):
        from agents.coder import run_coder_node

        await run_coder_node(state, cast(Any, cfg))

    assert collected == []


@pytest.mark.anyio
async def test_coder_reasoning_pass_stays_off_when_the_toggle_is_off() -> None:
    """No configurable at all (the shape every other test in this file uses)
    must behave exactly as before this addition — no reasoning call attempted."""
    mission = _make_mission([_make_step()])
    state = _make_state(mission, step_id=1)
    mock_reasoning = MagicMock()

    with patch("tools.llm_gateway.LLMGateway.astream_reasoning", mock_reasoning):
        from agents.coder import run_coder_node

        result = await run_coder_node(state)

    mock_reasoning.assert_not_called()
    assert result.get("mission_spec") is not None


# ── Test F — SEARCH/REPLACE block parser ──────────────────────────────────────


def test_parse_single_edit() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits == [
        {"file_path": "main.py", "search_block": "    return 1", "replace_block": "    return 2"}
    ]


def test_parse_multiple_edits() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        "### EDIT b.py\n<<<<<<< SEARCH\ny = 3\n=======\ny = 4\n>>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert len(edits) == 2
    assert edits[0]["file_path"] == "a.py" and edits[0]["replace_block"] == "x = 2"
    assert edits[1]["file_path"] == "b.py" and edits[1]["search_block"] == "y = 3"


def test_parse_new_file_empty_search() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT new.py\n"
        "<<<<<<< SEARCH\n"
        "=======\n"
        "def hello():\n    return 'hi'\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == ""
    assert edits[0]["replace_block"] == "def hello():\n    return 'hi'"


def test_parse_tolerates_surrounding_prose() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "Sure, here is the edit you asked for:\n\n"
        "### EDIT main.py\n<<<<<<< SEARCH\na = 1\n=======\na = 2\n>>>>>>> REPLACE\n\n"
        "Let me know if you need anything else!\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits == [
        {"file_path": "main.py", "search_block": "a = 1", "replace_block": "a = 2"}
    ]


def test_parse_strips_border_blank_lines() -> None:
    from agents.coder import _parse_search_replace_blocks

    # Model padded the body with blank lines after the SEARCH marker and before
    # the divider; _clean_block must strip them so the EXACT match still lands.
    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "\n"
        "    return 1\n"
        "\n"
        "=======\n"
        "\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == "    return 1"
    assert edits[0]["replace_block"] == "    return 2"


def test_parse_peels_accidental_markdown_fence() -> None:
    from agents.coder import _parse_search_replace_blocks

    text = (
        "### EDIT main.py\n"
        "<<<<<<< SEARCH\n"
        "```python\n"
        "    return 1\n"
        "```\n"
        "=======\n"
        "```\n"
        "    return 2\n"
        "```\n"
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert edits[0]["search_block"] == "    return 1"
    assert edits[0]["replace_block"] == "    return 2"


def test_parse_keeps_internal_backticks() -> None:
    from agents.coder import _parse_search_replace_blocks

    # A body that merely CONTAINS a fence line internally (not wrapping it) must
    # not be corrupted — the first line is real code, not a fence opener.
    text = (
        "### EDIT doc.py\n"
        "<<<<<<< SEARCH\n"
        "x = 1\n"
        "=======\n"
        'README = """\n```\ncode\n```\n"""\n'
        ">>>>>>> REPLACE\n"
    )
    edits = _parse_search_replace_blocks(text)
    assert "```" in edits[0]["replace_block"]
    assert edits[0]["replace_block"].startswith('README = """')


@pytest.mark.anyio
async def test_coder_exact_patch_survives_leading_blank_line() -> None:
    """End-to-end: a model-padded SEARCH block still lands an EXACT patch (no fuzzy)."""
    from core.vfs_middleware import VFSReadResult
    from agents.coder import run_coder_node

    content = "def calculate(x):\n    return x + 1\n"
    # Blank line right after the SEARCH marker — _clean_block must remove it so the
    # anchor matches the file verbatim through apply_search_replace Pass 1.
    edit_blob = (
        "### EDIT calc.py\n"
        "<<<<<<< SEARCH\n"
        "\n"
        "    return x + 1\n"
        "=======\n"
        "    return x + 2\n"
        ">>>>>>> REPLACE\n"
    )
    step = _make_step(action="edit_file", target_file="calc.py", description="Bump increment.")
    state = _make_state(_make_mission([step]))

    with patch(
        "core.vfs_middleware.VFSMiddleware.read_safe",
        return_value=VFSReadResult(content=content),
    ), patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=_fake_llm_response(edit_blob)),
    ):
        result = await run_coder_node(state)

    assert result["pending_contents"]["calc.py"] == "def calculate(x):\n    return x + 2\n"
