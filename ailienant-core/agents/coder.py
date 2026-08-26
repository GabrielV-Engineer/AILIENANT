# ailienant-core/agents/coder.py

import asyncio
import difflib
import hashlib
import logging
import os
import re
import uuid
from typing import Any, Callable, Dict, Optional, Set

from langchain_core.runnables import RunnableConfig

from brain.state import WBSStep
# role registry lives in agents/roles.py (flat-module import via conftest).
from agents.roles import build_coder_system_prompt, get_role_config
from agents.prompts import build_boundary_declaration
# Durable, immutable WBS-step status writer — the graph checkpoints every
# super-step, so a step transition MUST be a returned state delta (never an
# in-place mutation) or it is lost to Time-Travel and the multi-step loop.
from agents.orchestrator import _mark_step_status
from core.project_instructions import get_project_instructions
from brain.agent_context import (
    AMNESIA_ALERT,
    build_agent_context,
    resolve_context_budget,
    resolve_output_budget,
    resolve_real_window,
)
from brain.context_pipeline import ContextBudgetError

logger = logging.getLogger("CODER_NODE")

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: Set[asyncio.Task[Any]] = set()


def content_hash(s: str) -> str:
    """SHA-256 over newline- and BOM-normalized text.

    Python text-mode reads collapse CRLF→LF, while VS Code's doc.getText() keeps
    the editor EOL. Normalizing both sides before hashing prevents every Windows
    (CRLF) file from falsely reading as stale at apply time.

    Separately: `open(path, encoding="utf-8")` decodes a leading UTF-8 BOM as a
    literal U+FEFF character (only `encoding="utf-8-sig"` strips it), while VS
    Code's `TextDocument.getText()` never includes the BOM. Any file saved with a
    BOM (e.g. PowerShell's `Out-File`/`Set-Content`, which default to BOM-prefixed
    UTF-8) would otherwise hash differently here than on the host's side of the
    stale guard — a permanent, deterministic false "changed since the proposal"
    for that file regardless of whether anything actually changed. Stripping the
    BOM before hashing keeps both sides comparing the same logical text.
    """
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Coder generation output ceiling. Scale by two cheap, independent complexity
# signals instead of a flat constant — a global bump raises cost/latency on every
# trivial rename: the size of the file being touched (a bigger file needs more
# room to both anchor SEARCH blocks and emit REPLACE content) and the step's
# description length (a longer, more detailed instruction usually asks for more
# code). This is the DECLARED ceiling only — what the coder would LIKE to have
# room for. The actual max_tokens sent to the model is
# `resolve_output_budget`'s result (brain/agent_context.py), which reconciles
# this ceiling against the model's REAL served window and the REAL measured
# prompt. A flat `min(ceiling, budget // 2)` shape used to collapse onto its own
# floor at the only budget the system ever actually resolved, handing every step
# the identical max_tokens regardless of its own complexity or the model's real
# capacity — see resolve_output_budget's docstring.
_CODER_MIN_MAX_TOKENS: int = 4096
_CODER_MAX_MAX_TOKENS: int = 16384

# Token ceiling for the coder's own pre-generation reasoning pass (non-native
# models only — mirrors agents/planner.py's _PLANNER_REASONING_MAX_TOKENS).
# Small on purpose: a conceptual narrative shown while the user waits, never
# the edit itself — the strict SEARCH/REPLACE generation that follows carries
# the real output budget and is never scaffolded (DEBT-013 invariant).
_CODER_REASONING_MAX_TOKENS: int = 512


def _coder_declared_ceiling(target_step: WBSStep, current_content: Optional[str]) -> int:
    """The coder's own complexity-scaled ceiling, before reconciling it against
    the real window (see `resolve_output_budget`).

    Never raises — any unexpected input (e.g. a malformed step) degrades to the
    flat minimum.
    """
    try:
        is_new_file = target_step.action == "write_file" or current_content is None
        if is_new_file:
            # A new file IS the entire REPLACE-side output; scale with how much
            # the task description asks for.
            scaled = _CODER_MIN_MAX_TOKENS + len(target_step.description or "") * 4
        else:
            # An edit's output is bounded by how much of the existing file it
            # must reproduce/touch; the file's total size is the cheapest proxy.
            scaled = _CODER_MIN_MAX_TOKENS + len(current_content or "") // 2
        return int(min(_CODER_MAX_MAX_TOKENS, max(_CODER_MIN_MAX_TOKENS, scaled)))
    except Exception:  # noqa: BLE001 — a ceiling-derivation fault must not block generation
        logger.debug("coder: declared-ceiling scaling failed; falling back to flat minimum", exc_info=True)
        return _CODER_MIN_MAX_TOKENS


def _make_vfs_reader(project_id: str, workspace_root: str, session_id: str) -> Callable[[str], Optional[str]]:
    """Return a callable(path) -> Optional[str] backed by the VFS firewall."""
    from core.vfs_middleware import make_safe_reader
    return make_safe_reader(project_id, workspace_root, session_id)


def _format_read_size(byte_count: int) -> str:
    """Compact human size for a Glass-Box Timeline 'read' row's metric field."""
    if byte_count < 1024:
        return f"{byte_count} B"
    return f"{byte_count / 1024:.1f} KB"


# ── SEARCH/REPLACE edit parsing ────────────────────────────────────────────────
# The model emits edits as git-conflict-style blocks instead of JSON. Code lives
# verbatim between the markers, so it is never escaped — eliminating the class of
# json.loads failures that arise when a model fails to escape quotes/newlines in a
# code string value.

_EDIT_HEADER = "### EDIT"
_SR_SEARCH = "<<<<<<< SEARCH"
_SR_DIVIDER = "======="
_SR_REPLACE = ">>>>>>> REPLACE"
_FENCE_OPEN_RE = re.compile(r"^```[\w-]*$")


def _clean_block(lines: list[str]) -> str:
    """Border-harden a parsed block so apply_search_replace hits the EXACT pass.

    apply_search_replace matches by exact then per-line-rstrip-normalized substring;
    neither pass strips blank lines at the block borders. A leading/trailing newline
    left by the parser would therefore drop the patch to the risky fuzzy fallback or
    fail it outright. strip("\\n") (NOT strip(), which would eat the first line's
    indentation) removes those border newlines. A precise per-line fence check also
    peels one accidental wrapping markdown fence the model may have added, without
    touching code that merely contains backticks internally.
    """
    text = "\n".join(lines).strip("\n")
    parts = text.splitlines()
    if len(parts) >= 2 and _FENCE_OPEN_RE.match(parts[0].strip()) and parts[-1].strip() == "```":
        text = "\n".join(parts[1:-1]).strip("\n")
    return text


def _split_glued_terminator(line: str) -> Optional[str]:
    """Return the code part of a line whose tail is a REPLACE terminator, else ``None``.

    A model intermittently emits the closing marker without the newline that should
    precede it (``export default App;>>>>>>> REPLACE``). A strict line-equality scan
    never matches such a line, so the terminator — and everything after it — is
    swallowed into the replacement body and written to disk verbatim, producing a
    source file containing a literal conflict marker. Recognising the glued form
    recovers the real content instead of discarding a whole valid edit.
    """
    stripped = line.rstrip()
    if stripped != _SR_REPLACE and stripped.endswith(_SR_REPLACE):
        return stripped[: -len(_SR_REPLACE)]
    return None


def _parse_search_replace_blocks(text: str) -> list[dict[str, str]]:
    """Parse SEARCH/REPLACE edit blocks into {file_path, search_block, replace_block}.

    Code between the markers is taken verbatim — never JSON-escaped — so it may
    contain any quote, newline, or backslash. Tolerant of prose or markdown fences
    before/after/between blocks: only the four marker lines are structural.

    A block is emitted ONLY once its closing terminator is seen. An unterminated
    block means the generation was cut short mid-edit; committing what arrived
    would write a silently partial file that every downstream gate accepts as
    complete. Such a block is dropped, and blocks parsed before it are unaffected.
    """
    edits: list[dict[str, str]] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip().startswith(_EDIT_HEADER):
            file_path = lines[i].strip()[len(_EDIT_HEADER):].strip()
            i += 1
            while i < n and lines[i].strip() != _SR_SEARCH:
                i += 1
            if i >= n:
                break
            i += 1
            search: list[str] = []
            while i < n and lines[i].strip() != _SR_DIVIDER:
                search.append(lines[i])
                i += 1
            if i >= n:
                break
            i += 1
            replace: list[str] = []
            terminated = False
            while i < n:
                if lines[i].strip() == _SR_REPLACE:
                    terminated = True
                    break
                glued = _split_glued_terminator(lines[i])
                if glued is not None:
                    replace.append(glued)
                    terminated = True
                    break
                replace.append(lines[i])
                i += 1
            if not terminated:
                # Ran off the end of the response: the model never closed this block.
                # Drop it rather than applying a truncated edit as if it were whole.
                logger.warning(
                    "Coder: dropping an unterminated SEARCH/REPLACE block for %r "
                    "(no '%s' marker before end of response — the generation was cut "
                    "short). %d earlier edit(s) in this response are unaffected.",
                    file_path, _SR_REPLACE, len(edits),
                )
                break
            if file_path:
                edits.append({
                    "file_path": file_path,
                    "search_block": _clean_block(search),
                    "replace_block": _clean_block(replace),
                })
        i += 1
    return edits


async def _fetch_rag_snippets(
    target_file: str,
    description: str,
    project_id: str,
    retrieval_fn: Any = None,
    explicit_mentions: Optional[list[str]] = None,
    workspace_root: str = "",
) -> list[tuple[str, str]]:
    """Single GraphRAG retrieval shared by the topology and style blocks.

    Fetching once (vs. once per block) avoids a redundant embedding call against
    the vector store. ``retrieval_fn`` is an optional injectable override (the
    default is the real ``search_snippets``); a benchmark supplies a degraded
    variant. Best-effort: returns [] on missing project or any failure.

    Results are filtered through ``filter_relevant_snippets`` (keyed off
    ``target_file``) so a workspace root spanning two unrelated projects never
    injects the other project's code into this one — see core/utils.py.
    """
    if not project_id:
        return []
    try:
        from core.memory.semantic_memory import SemanticMemoryManager
        from core.utils import filter_relevant_snippets
        _search_snippets = retrieval_fn or SemanticMemoryManager().search_snippets
        raw = await _search_snippets(
            f"{target_file} {description}", workspace_hash=project_id, k=3,
            project_root=workspace_root or None,
        )
        return filter_relevant_snippets(raw, target_file, explicit_mentions)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Coder RAG fetch failed (non-fatal): %s", exc)
        return []


def _build_rag_block(snippets: list[tuple[str, str]]) -> str:
    """GraphRAG topology block for the coder system prompt (best-effort)."""
    blocks = "\n\n".join(f"### {p}\n{s}" for p, s in snippets if s)
    if not blocks:
        return ""
    return (
        "\n\n# Relevant workspace context (GraphRAG)\n"
        "Excerpts from the project that may help you write a correct edit.\n\n" + blocks
    )


def _build_style_block(target_file: str, snippets: list[tuple[str, str]]) -> str:
    """Few-Shot code-STYLE block: AST skeletons of same-language project functions.

    Distinct from the topology block — this teaches house convention (signatures,
    type hints, docstrings) with bodies elided, never logic. Filters the shared
    snippets to the target file's language, distills each to a skeleton, and frames
    them under STYLE_EXEMPLAR_HEADER. Best-effort: returns '' when nothing usable.
    """
    from shared.contracts import detect_language
    from core.ast_engine import extract_skeleton
    from agents.prompts import STYLE_EXEMPLAR_HEADER

    lang = detect_language(target_file)
    if not lang:
        return ""
    skeletons = [
        skel
        for path, snippet in snippets
        if snippet and detect_language(path) == lang
        for skel in (extract_skeleton(snippet, lang),)
        if skel
    ][:3]
    if not skeletons:
        return ""
    return STYLE_EXEMPLAR_HEADER + "\n\n".join(skeletons)


# ── READ_ONLY tool-grounding pre-pass (DEBT-130) ────────────────────────────────
# The one-shot SEARCH/REPLACE path handles the majority of WBS steps by volume and
# had zero tool-calling — it reasoned only from the current file + GraphRAG
# snippets, guessing when both came back thin. This bounded pre-pass reuses the
# same select_tools -> resolve_tools -> ToolDispatcher substrate the agentic cell
# and dispatched subagents use, filtered to a READ_ONLY tier ceiling: mutation
# stays the cell's surface (DEBT-068's standing ruling), and this path is
# re-entered by the error_correction retry loop, so only an idempotent READ_ONLY
# pass is safe here without a HITL approval channel.

_GROUNDING_MAX_ITERS: int = 2


def _needs_grounding(
    target_step: WBSStep,
    current_content: Optional[str],
    rag_snippets: list[tuple[str, str]],
    state: Dict[str, Any],
) -> bool:
    """Skip the extra reasoning round-trip when the step is already grounded.

    Fires for the cases DEBT-130 actually names — a new file, GraphRAG returning
    nothing usable, or a retry after failed validation — not the trivial majority
    of already-grounded steps, so cost/latency stays flat where help isn't needed.
    """
    if current_content is None:
        return True
    if not any(snippet for _path, snippet in rag_snippets):
        return True
    if state.get("validation_feedback"):
        return True
    return False


def _grounding_admitted(session_mode: Any) -> bool:
    """True only when a READ_ONLY tool actually resolves to ALLOW under the
    current session policy.

    Every canonical mode except ASK_ALL allows READ_ONLY unconditionally, but
    ASK_ALL resolves it to HITL — and this pre-pass never wires an approval
    channel (see the module note above), so under ASK_ALL every candidate call
    would come back deny-with-report. Skipping here avoids paying a full
    reasoning round-trip to gather nothing.
    """
    from core.permissions import PermissionDecision, ToolPrivilegeTier, evaluate_action
    from shared.rbac import PermissionMode

    decision = evaluate_action(
        session_mode, ToolPrivilegeTier.READ_ONLY, PermissionMode.EDIT_EXECUTE_RBW
    )
    return decision is PermissionDecision.ALLOW


async def _run_grounding_loop(
    target_step: WBSStep, state: Dict[str, Any], session_id: str
) -> str:
    """Bounded READ_ONLY tool-calling pass feeding observations back as context.

    A separate, small reasoning call — distinct from the strict single-shot
    SEARCH/REPLACE generation, which is never scaffolded with tool-calling of its
    own. Tier-filtered to READ_ONLY before the loop ever sees a schema, so a
    WRITE/EXECUTE/DANGEROUS tool can never be proposed here regardless of what
    the role's RBAC would otherwise permit. Never fatal: any selection,
    resolution, or dispatch failure degrades to an empty grounding block,
    identical to the pre-existing (no tool-calling) behavior.
    """
    try:
        from core.permissions import ToolPrivilegeTier, session_mode_from_channel
        from core.tool_dispatch import ToolDispatcher, make_gateway_reasoner
        from core.tool_rag import TOOL_RAG_TOP_K, tool_rag_store
        from core.tool_registry import filter_loop_safe, resolve_tools
        from shared.rbac import PermissionMode

        session_mode = session_mode_from_channel(state.get("session_permission_mode"))
        if not _grounding_admitted(session_mode):
            return ""

        active_role = target_step.target_role or "core_dev"
        intent = f"{target_step.action} {target_step.target_file} {target_step.description}"
        schemas = await tool_rag_store.select_tools(
            intent, k=TOOL_RAG_TOP_K, active_role=active_role, session_mode=session_mode,
        )
        # ask_user_question is READ_ONLY by permission tier but structurally cannot
        # suspend here (DEBT-171): this pre-pass has no defer/resume phase and is
        # re-entered by the error_correction retry loop, so offering a tool that
        # cannot actually pause the turn is the same "tool that lies" defect the
        # channel itself used to be. Excluded regardless of tier.
        #
        # It stays a local exclusion rather than joining _NO_AUTONOMOUS_LOOP: the
        # predicate there is "inert in ANY reasoning loop", while this one is
        # "this particular loop has no suspend phase" — ask_user_question works
        # correctly in the agentic cell, which does have one.
        #
        # filter_loop_safe covers the other direction: tools that survive the
        # READ_ONLY filter yet do nothing when a loop calls them. select_tools's
        # READ_ONLY-survivor guarantee actively promotes such a tool into the
        # selection when the ranking has no other READ_ONLY candidate, so this
        # pre-pass is more exposed to them than the tier filter suggests.
        read_only_schemas = [
            s for s in filter_loop_safe(schemas)
            if s.privilege_tier is ToolPrivilegeTier.READ_ONLY and s.name != "ask_user_question"
        ]
        if not read_only_schemas:
            return ""

        tools = resolve_tools(read_only_schemas, state)
        if not tools:
            return ""

        dispatcher = ToolDispatcher(
            tools,
            active_role=active_role,
            session_mode=session_mode,
            state=state,
            agent_permission=PermissionMode.EDIT_EXECUTE_RBW,
            approval_fn=None,
        )
        reasoner = make_gateway_reasoner(tools, session_id=session_id)
        messages: list[Dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Task: {target_step.description}\n"
                    f"Target file: {target_step.target_file}\n"
                    "Gather any READ_ONLY context you need before the edit is "
                    "generated; emit {} once you have enough."
                ),
            }
        ]
        trace: list[Any] = []
        await dispatcher.run_loop(
            messages, reasoner, max_iters=_GROUNDING_MAX_ITERS, trace=trace
        )
        observations = [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "system"
            and str(m.get("content", "")).startswith("[tool observations]")
        ]
        if not observations:
            return ""
        return "\n\n# Tool-grounding observations\n" + "\n\n".join(observations)
    except Exception as exc:  # noqa: BLE001 — a grounding-loop fault must not block generation
        logger.debug(
            "CoderAgent grounding pre-pass failed (non-fatal): %s", exc, exc_info=True
        )
        return ""


async def run_coder_node(state: Dict[str, Any], config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    LangGraph node: El Ejecutor (CoderAgent)

    Structured single-shot: the LLM returns a JSON list of AtomicPatch edits
    ({file_path, search_block, replace_block}) for the active WBS step. Edits are
    validated and applied to an IN-MEMORY copy only (no disk, no RAM-VFS write) to
    compute a unified diff per file, which is returned in `pending_patches` for
    propose-&-review. Nothing is written to the user's files in this phase.
    """
    validation_feedback = state.get("validation_feedback")
    if validation_feedback:
        logger.info(
            "CoderAgent: retrying with guardrail feedback (retry %d)",
            state.get("retry_count", 0),
        )

    step_id: int | None = state.get("current_step_id")
    mission_spec = state.get("mission_spec")

    # 13.0.9 — a "request changes" verdict from the apply-gate's interrupt-based
    # approval. Scoped to THIS step via an explicit step_number match: a stale
    # comment left over from an earlier step's regeneration (state is a single
    # shared channel, scalar-overwrite) must never leak into a later step's
    # prompt. Cleared unconditionally in the result below regardless of whether
    # it matched, so a mismatched/stale entry can never survive past one node
    # execution either.
    _apply_feedback_raw = state.get("apply_feedback")
    apply_feedback: Optional[str] = None
    if (
        isinstance(_apply_feedback_raw, dict)
        and _apply_feedback_raw.get("step_number") == step_id
    ):
        apply_feedback = _apply_feedback_raw.get("comment") or None

    if mission_spec is None:
        logger.error("CoderAgent invoked without mission_spec in state.")
        return {"errors": ["CoderAgent: mission_spec missing — aborting step."]}

    target_step: WBSStep | None = next(
        (t for t in mission_spec.tasks if t.step_number == step_id),
        None,
    )
    if target_step is None:
        logger.error("CoderAgent: step_id=%s not found in mission_spec.", step_id)
        return {"errors": [f"CoderAgent: WBSStep #{step_id} It's not in the plan."]}

    logger.info(
        "⚙️  CoderAgent executing step #%d [%s] → %s",
        target_step.step_number, target_step.target_role, target_step.target_file,
    )

    session_id: str = state.get("task_id", "")
    project_id: str = state.get("project_id") or ""
    workspace_root: str = state.get("workspace_root") or ""
    target_file: str = target_step.target_file

    role_cfg = get_role_config(target_step.target_role)
    _role_overrides = state.get("agent_role_overrides") or {}
    system_prompt: str = build_coder_system_prompt(
        target_step.target_role,
        override=_role_overrides.get(target_step.target_role or ""),
    )

    # Freeform project instructions (AILIENANT.md) — standing implementation
    # guidance (conventions, domain notes) the coder honors on every step.
    _project_instructions = get_project_instructions(project_id, workspace_root, session_id)
    if _project_instructions:
        system_prompt += f"\n\n{_project_instructions}"

    # Pre-execution HITL gates — emit security flags when the active step matches a
    # role-specific HITL trigger (e.g. devops_infra touching .env, vcs_manager --force).
    new_security_flags: list[str] = []
    task_blob = f"{target_step.target_file} {target_step.description}"
    for trigger in role_cfg["hitl_triggers"]:
        if trigger in task_blob:
            new_security_flags.append(
                f"HITL_APPROVAL_REQUIRED:{target_step.target_role}:{trigger}"
            )

    from api.websocket_manager import vfs_manager  # deferred — avoids circular import

    # Granular sub-step narration. task_service injects an async emitter on
    # config.configurable["narrate"] (kept off graph state so the checkpointer never
    # serializes a callable); the coder stays decoupled from the transport layer
    # (never imports the WS manager for this) — the cognitive-isolation fence holds.
    _narrate = (config or {}).get("configurable", {}).get("narrate")
    # Reasoning sink (Thought Box) + native-thinking prefs, same off-state seam.
    _on_thinking = (config or {}).get("configurable", {}).get("stream_thinking")
    _thinking_on = bool((config or {}).get("configurable", {}).get("enable_native_thinking"))
    _thinking_budget = int((config or {}).get("configurable", {}).get("thinking_budget_tokens") or 4096)
    # Snippet retrieval is injectable so a benchmark can degrade it explicitly;
    # production omits this key and the real bound method runs unchanged.
    _coder_retrieval_fn = (config or {}).get("configurable", {}).get("coder_retrieval_fn")

    async def _emit(node_name: str, metric: Optional[str] = None) -> None:
        if _narrate is not None:
            await _narrate(node_name, metric=metric)

    # read_file produces nothing to patch — the context it gathers is already
    # folded into the running state, so the step genuinely completed.
    if target_step.action == "read_file":
        # Flip the IDE checklist row (read-only steps were previously silent).
        _t = asyncio.create_task(
            vfs_manager.emit_graph_mutation(
                session_id=session_id,
                step_number=target_step.step_number,
                new_status="completed",
                agent_name="CoderAgent",
            )
        )
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)
        return {
            "mission_spec": _mark_step_status(
                mission_spec, target_step.step_number, "completed"
            ),
            "current_step_id": target_step.step_number,
            "target_role": target_step.target_role,
            **({"security_flags": new_security_flags} if new_security_flags else {}),
        }

    # run_command stages the step's command for the apply gate
    # (brain/apply_gate.py, 13.0.9) — permission verdict, HITL approval, the
    # dangerous-pattern guard, execution itself, and self-heal on a non-zero
    # exit all moved downstream. This node's job is narrower now: confirm
    # there is an adapter to run on at all, and refuse a hygiene-invalid
    # command outright (closes a live-reproduced bug: "N/A", a placeholder the
    # WBS schema's overloaded target_file field produced when the planner had
    # no concrete command to give, reached a real shell before this check
    # existed). For a run_command step the command lives in target_file (the
    # schema overloads it: "path of the affected file ... OR command to run").
    if target_step.action == "run_command":

        def _notify_status(new_status: str) -> None:
            # Fire-and-forget IDE chip update. The synchronous step status write is
            # atomic w.r.t. the loop (no await between read and write); the returned
            # dict is the authoritative transition the reducer applies on node exit.
            _t = asyncio.create_task(
                vfs_manager.emit_graph_mutation(
                    session_id=session_id,
                    step_number=target_step.step_number,
                    new_status=new_status,
                    agent_name="CoderAgent",
                )
            )
            _background_tasks.add(_t)
            _t.add_done_callback(_background_tasks.discard)

        from core.sandbox import resolve_execution_adapter

        # Trusted project execution: prefer the user's devcontainer (with a
        # HITL-gated native fallback) when a session is live; else the oracle tier.
        # Only a capability PROBE here — the apply gate re-resolves the adapter
        # itself at actuation time; this just avoids staging a command that has
        # nowhere to run at all.
        adapter = resolve_execution_adapter(session_id=session_id, trusted=True)

        # No resolved tier → nothing to spawn into. Marking it "completed" would lie
        # that a command ran; surface it honestly as failed-and-deferred. This is the
        # operator-honesty contract — it holds ONLY when no adapter exists.
        if adapter is None:
            new_security_flags.append(
                f"EXECUTE_TIER_DEFERRED:{target_step.target_role}:{target_step.target_file}"
            )
            _notify_status("failed")
            return {
                "mission_spec": _mark_step_status(
                    mission_spec, target_step.step_number, "failed"
                ),
                "current_step_id": target_step.step_number,
                "target_role": target_step.target_role,
                "errors": [
                    f"CoderAgent step #{target_step.step_number}: run_command was NOT "
                    "executed — no sandbox adapter is active."
                ],
                "security_flags": new_security_flags,
            }

        from tools.execution_tools import validate_step_command

        command, validation_error = validate_step_command(target_step.target_file)
        if command is None:
            await _emit(f"blocked {target_step.target_file}")
            _notify_status("failed")
            return {
                "mission_spec": _mark_step_status(
                    mission_spec, target_step.step_number, "failed"
                ),
                "current_step_id": target_step.step_number,
                "target_role": target_step.target_role,
                "errors": [
                    f"CoderAgent step #{target_step.step_number}: run_command "
                    f"refused before execution — {validation_error}"
                ],
                **({"security_flags": new_security_flags} if new_security_flags else {}),
            }

        _notify_status("in_progress")
        return {
            "mission_spec": _mark_step_status(
                mission_spec, target_step.step_number, "in_progress"
            ),
            "pending_step_command": {str(target_step.step_number): command},
            "current_step_id": target_step.step_number,
            "target_role": target_step.target_role,
            **({"security_flags": new_security_flags} if new_security_flags else {}),
        }

    # 1. Context assembly: current file + GraphRAG snippets. One retrieval feeds
    # both the topology block (relevant context) and the style block (house
    # convention exemplars) so the vector store is hit only once.
    _read_vfs = _make_vfs_reader(project_id, workspace_root, session_id)
    # Prefer a prior in-run step's edit of this file (the multi-step loop) so the
    # model refactors the ALREADY-edited version, not the stale committed one.
    _prior_this_file = (state.get("pending_contents") or {}).get(target_file)
    current_content = _prior_this_file if _prior_this_file is not None else _read_vfs(target_file)

    # Surface the file the coder inspected so the Glass-Box Timeline shows live
    # read activity; basename keeps the workspace path private. Emitted AFTER
    # the read (not before) so the row's metric carries the real byte size —
    # only available once the content is in hand. A tool-initiated read (the
    # `read_file` registry tool) is covered separately by the tool-dispatch
    # detail body, not this marker — this one is the coder's own direct,
    # pre-generation read of its `target_file`, which passes through no
    # dispatcher.
    _read_metric = _format_read_size(len(current_content.encode("utf-8"))) if current_content is not None else None
    await _emit(f"reading {os.path.basename(target_file)}", metric=_read_metric)

    rag_snippets = await _fetch_rag_snippets(
        target_file, target_step.description, project_id, _coder_retrieval_fn,
        explicit_mentions=state.get("explicit_mentions"),
        workspace_root=workspace_root,
    )
    # Surface the GraphRAG lookup on the Glass-Box Timeline — the "retrieval"
    # kind was declared on the wire from the start but nothing ever emitted
    # it. Gated on project_id (matching _fetch_rag_snippets' own short-circuit)
    # so a project-less call never claims a lookup that never ran; emitted
    # AFTER the fetch so the metric reflects the real hit count.
    if project_id:
        await _emit(
            f"retrieving {os.path.basename(target_file)}",
            metric=f"{len(rag_snippets)} snippet(s)",
        )
    rag_block = _build_rag_block(rag_snippets)
    style_block = _build_style_block(target_file, rag_snippets)

    # Bounded READ_ONLY tool-grounding pre-pass (DEBT-130): only when the file+RAG
    # context above is thin (new file, empty RAG, or a retry after failed
    # validation) does the coder pay a second reasoning round-trip to gather more
    # context before generating the edit. Never mutating and never gated behind
    # HITL by construction (see _needs_grounding/_grounding_admitted docstrings),
    # so this path stays idempotent under the error_correction retry re-entry —
    # mutation itself stays on the agentic cell's surface, unchanged.
    grounding_block = ""
    if _needs_grounding(target_step, current_content, rag_snippets, state):
        grounding_block = await _run_grounding_loop(target_step, state, session_id)

    boundary = uuid.uuid4().hex
    # Declared once, appended unconditionally to the system message below (both
    # the success and ContextBudgetError-degrade paths) — see
    # build_boundary_declaration's docstring for why this must never be folded
    # into a budget-guarded, silently-droppable layer. `system_prompt` above
    # (build_coder_system_prompt + _project_instructions) stays nonce-free and
    # byte-identical across calls for the same role/workspace — the cacheable
    # prefix.
    _boundary_decl = build_boundary_declaration(boundary)

    # User skill injection — skills the user saved and either explicitly invoked
    # or that matched this task semantically, resolved once at task init and
    # threaded on state. Wrapped in the same ephemeral boundary as the planner so
    # the coder honors the same standing directives. Mirrors agents/planner.py.
    # Kept as an unconditional post-pipeline append (not routed through the
    # budget-guarded pipeline layers) to preserve the pre-existing behavior that
    # skill directives are never silently dropped under budget pressure.
    _skill_block = ""
    _skills = state.get("active_skills") or []
    if _skills:
        from core.skill_resolver import build_skill_directive_block

        _skill_block = build_skill_directive_block(_skills, boundary)

    if current_content is not None:
        file_block = f'<{boundary} filepath="{target_file}">\n{current_content}\n</{boundary}>'
    else:
        file_block = f"(The file {target_file} does not exist yet — you will create it.)"

    # 13.0.9 — a human's "request changes" reply, scoped to this exact step
    # (matched above). Placed right after the task line, before the boundary/
    # context material, so it reads as a direct amendment to the task rather
    # than being buried after the file content.
    _apply_feedback_block = (
        f"\nReviewer feedback on your previous attempt at this step — address "
        f"it in this regeneration: {apply_feedback}\n"
        if apply_feedback else ""
    )

    # A coder-only tool-usage constraint a post-generation annotator attached
    # (e.g. the polyglot-file patch-tool requirement, agents/planner.py) —
    # deliberately kept out of `description` itself so it never reaches the
    # human-facing checklist, a semantic-cache key, or a retrieval query.
    _agent_notes_block = (
        f"\nAgent directive for this step: {target_step.agent_notes}\n"
        if target_step.agent_notes else ""
    )

    # Task preamble + format postamble bracket the budget-guarded context block so
    # the model sees: task → (current file + RAG topology + style exemplars) →
    # output-format rules, preserving the original ordering after the splice.
    _task_preamble = (
        f"WBS step #{target_step.step_number} — role {target_step.target_role}, "
        f"action {target_step.action}.\nTarget file: {target_file}\n"
        f"Task: {target_step.description}\n"
        f"{_agent_notes_block}"
        f"{_apply_feedback_block}\n"
        # Bare reference only — no axiom/declaration language here (SEAL2): the
        # sandbox rule and which tag is authoritative are declared exclusively in
        # the system message via build_boundary_declaration(), a trusted-only
        # channel untrusted content can never write to. Restating the rule here,
        # in the same message role as the untrusted content it wraps, would let
        # injected text forge a competing declaration with no structural way for
        # the model to prefer the real one.
        f"The current file content and relevant project context follow inside the "
        f"<{boundary}> tags below.\n\n"
    )
    _format_postamble = (
        "Return ONLY one or more SEARCH/REPLACE edit blocks in EXACTLY this format "
        "(no JSON, no markdown fences, no prose before or after):\n\n"
        "### EDIT <file_path>\n"
        "<<<<<<< SEARCH\n"
        "<verbatim code to replace>\n"
        "=======\n"
        "<new code>\n"
        ">>>>>>> REPLACE\n\n"
        "Rules: the SEARCH section MUST be copied verbatim from the current content "
        "and be a unique anchor of at least 10 non-whitespace characters. For a NEW "
        "file, leave the SEARCH section empty and put the full file content in the "
        "REPLACE section. Emit one block per edit; keep edits minimal and correct; "
        "only touch the target file. Write the code literally between the markers — "
        "do NOT escape or wrap it."
    )

    # Mission-level decisions/constraints (e.g. the stack chosen in planner.py's
    # _STACK_GUIDANCE_DIRECTIVE) never reached this prompt before — mission_spec was read
    # only for step lookup/status, so a correct planner-side choice could still drift per
    # step across a multi-step build. Bounded projection; "" when there is nothing to add.
    # (mission_spec is already guaranteed non-None here by the early return above.)
    _mission_block = mission_spec.to_context_block()

    # ── Budget-guarded assembly (five-layer ContextPipeline) ──
    # L1 foundation = identity+role+project-instructions (system_prompt; never
    # silently truncated, byte-identical across calls — the cacheable prefix).
    # The boundary declaration and skill directives are appended unconditionally
    # after assembly (see below), never routed through a truncatable layer. The
    # volatile current file, GraphRAG topology, and style exemplars are the
    # Execution layer (L5) — trimmed first when the window is tight. Mission
    # context leads L5 so it is the last chunk trimmed under pressure. A
    # single-shot coder turn carries no conversation list, so L4 stays empty and
    # on_compacted is omitted.
    _budget = resolve_context_budget(state)
    try:
        _agent_ctx = await build_agent_context(
            total_token_budget=_budget,
            foundation=[system_prompt],
            execution=[_mission_block, file_block, rag_block, style_block, grounding_block],
            session_id=session_id,
            session_start_time=state.get("session_start_time"),
        )
        # _boundary_decl (+ _skill_block, when present) are intentionally OUTSIDE
        # the pipeline's budget-guarded layers — appended here, unconditionally,
        # matching the pre-existing behavior where the skill splice always
        # survived (it used to be concatenated into `system_prompt` before this
        # call), and the security-critical sandbox seal must never be trimmable.
        _tail = _boundary_decl + (f"\n\n{_skill_block}" if _skill_block else "")
        _system_content = f"{_agent_ctx.foundation_block}\n\n{_tail}"
        _context_block = _agent_ctx.execution_block
    except ContextBudgetError:
        # Identity alone exhausts the window: degrade without silently dropping pinned
        # context, and alert the model to its partial amnesia. Plain assignment — never
        # a re-entrant build, so it cannot loop.
        logger.warning(
            "CoderAgent context budget exhausted by L1-L3 (budget=%d); degrading to "
            "identity-only prompt with an explicit context-loss alert.",
            _budget, exc_info=True,
        )
        _tail = _boundary_decl + (f"\n\n{_skill_block}" if _skill_block else "")
        _system_content = f"{system_prompt}\n\n{_tail}"
        _context_block = (
            f"{_mission_block}\n\n{file_block}\n\n{rag_block}\n\n{style_block}\n\n"
            f"{grounding_block}\n\n{AMNESIA_ALERT}"
        )

    instruction = _task_preamble + _context_block + "\n\n" + _format_postamble

    messages = [
        {"role": "system", "content": _system_content},
        {"role": "user", "content": instruction},
    ]

    # 2. Generate edits (BYOM-aware ainvoke → active preset, JSON mode).
    # Semantic response cache: an identical step over unchanged context returns
    # the prior model output with no network round-trip. The live (RAM-VFS)
    # current_content and RAG snippets fold into the key, so an unsaved edit
    # naturally produces a fresh key — no separate dirty-buffer bypass needed.
    from tools.llm_gateway import LLMGateway
    from shared.config import MODEL_BIG
    from core.response_cache import response_cache
    from core.memory.context_auditor import resolve_model_alias_for_routing

    # The Researcher's CSS/TCI routing cascade already computed a real decision
    # (LOCAL_SMALL/LOCAL_MEDIUM/LOCAL_BIG/CLOUD) before the coder ever runs —
    # this is what makes that decision actually select a model tier for
    # generation, instead of every step hardcoding BIG regardless of what the
    # router said (N9). BIG remains the fallback when no decision has been
    # computed yet, matching today's behaviour exactly in that case.
    _routing_decision = getattr(state.get("context_metrics"), "routing_decision", None)
    _coder_model = resolve_model_alias_for_routing(_routing_decision, default=MODEL_BIG)
    _coder_tier = _coder_model.split("/", 1)[1] if _coder_model.startswith("ailienant/") else "big"
    _coder_tier = _coder_tier if _coder_tier in ("small", "medium", "big") else "big"

    cache_context = [(target_file, current_content or "")] + [
        (p, s) for p, s in rag_snippets if s
    ]
    # Fold the resolved token budget into the key: identical inputs produce a
    # different budget-trimmed prompt under a different context window (local↔cloud
    # reroute), so a budget-blind key could serve a stale trim.
    cache_context.append(("<budget>", str(_budget)))
    # Fold the grounding pre-pass output in too — a step with identical file/RAG
    # inputs but different tool observations (e.g. a symbol lookup that changed
    # between runs) must not serve a stale generation from before the observation
    # existed.
    if grounding_block:
        cache_context.append(("<grounding>", grounding_block))
    # A "request changes" regeneration must never replay the exact attempt the
    # human just rejected — fold the feedback text into the key so it always
    # misses the cache and produces a genuinely new completion.
    if apply_feedback:
        cache_context.append(("<apply_feedback>", apply_feedback))
    cache_key = response_cache.build_key(
        intent=f"{target_step.action}|{target_file}|{target_step.description}",
        context=cache_context,
        project_id=project_id,
        model=_coder_model,
    )
    cache_paths = [target_file] + [p for p, _ in rag_snippets]
    try:
        # Probe (lock released before inference); on miss, run then store. The
        # gateway await never sits inside the cache lock.
        cached = response_cache.probe(cache_key)
        if cached is not None:
            content = cached
        else:
            # Live pre-generation reasoning pass — ONLY for a non-native model
            # with the toggle on. A native model already surfaces its own
            # reasoning on a separate channel inside acomplete_with_thinking
            # below; a second pass here would double the trace and the cost.
            # A non-native model's strict SEARCH/REPLACE generation cannot
            # safely carry a reasoning preamble (DEBT-013: it corrupts the
            # machine-parsed output), so — mirroring agents/planner.py's own
            # pre-draft reasoning pass exactly — it runs here as a separate,
            # free-form completion instead. Before this, enable_native_thinking
            # was a silent no-op on any non-native target (the toggle promised
            # "show me the model's reasoning" but a local/unsupported model
            # showed nothing at all during a coding turn, regardless of the
            # setting). Best-effort: a sink or generation fault never blocks
            # the actual edit; only a real abort propagates.
            if _on_thinking is not None and _thinking_on:
                from core.config.model_resolver import get_chat_target  # deferred — load order
                from tools.llm_gateway import supports_native_thinking

                _r_target = get_chat_target(_coder_tier)
                _r_native = _r_target is not None and await supports_native_thinking(_r_target)
                if not _r_native:
                    _reasoning_messages = [
                        {"role": "system", "content": _system_content},
                        {"role": "user", "content": (
                            "Before writing the edit, think out loud about your approach "
                            "to this step: what needs to change, why, and any risk or "
                            "edge case to watch for. Write concise, conceptual prose — "
                            "no code, no SEARCH/REPLACE blocks, no file dumps.\n\n"
                            f"{instruction}"
                        )},
                    ]
                    _sink_live = True
                    try:
                        async for _d in LLMGateway.astream_reasoning(
                            _reasoning_messages,
                            tier=_coder_tier,
                            temperature=0.0,
                            max_tokens=_CODER_REASONING_MAX_TOKENS,
                            session_id=session_id,
                            thinking_budget_tokens=_thinking_budget,
                            free_form_answer=True,
                        ):
                            if _sink_live and _d.text:
                                try:
                                    await _on_thinking(_d.text, _d.source)
                                except asyncio.CancelledError:
                                    raise
                                except Exception:  # noqa: BLE001 — best-effort stream
                                    logger.debug(
                                        "coder reasoning sink failed; latching off",
                                        exc_info=True,
                                    )
                                    _sink_live = False
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — reasoning is best-effort; never block the edit
                        logger.debug("coder reasoning pass failed (non-fatal)", exc_info=True)

            # Joint-budget pre-flight check: measure the REAL prompt against the
            # REAL served window and refuse honestly BEFORE the call rather than
            # discovering the shortfall as a silently truncated file several
            # minutes later — the same mechanism agents/planner.py applies to its
            # own structured draft. Raised as a plain exception so it flows
            # through this block's own except-clause below, producing the exact
            # established failure contract for this node (mission marked
            # "failed", the reason surfaced in `errors`) rather than a new shape.
            from tools.token_counter import PrecisionTokenCounter  # deferred — see file header note

            _real_window = await resolve_real_window(state, _coder_tier)
            _prompt_text = "\n".join(m.get("content", "") for m in messages)
            _prompt_tokens = PrecisionTokenCounter.estimate_with_buffer(_prompt_text, _coder_model)
            _budget_decision = resolve_output_budget(
                prompt_tokens=_prompt_tokens,
                real_window=_real_window,
                declared_ceiling=_coder_declared_ceiling(target_step, current_content),
            )
            if not _budget_decision.ok:
                raise ValueError(_budget_decision.reason)

            # Streams native reasoning to the Thought Box while generating; the
            # structured JSON answer is buffered and returned exactly as before.
            # On a non-reasoning model (or thinking off) this is a plain JSON-mode
            # ainvoke with zero behaviour change.
            content = await LLMGateway.acomplete_with_thinking(
                messages=messages,
                model=_coder_model,
                temperature=0.0,
                max_tokens=_budget_decision.max_tokens,
                session_id=session_id,
                state=state,
                on_thinking=_on_thinking,
                enable_thinking=_thinking_on,
                thinking_budget_tokens=_thinking_budget,
                # Tags real token usage with the WBS action it serves (DEBT-045's
                # calibration substrate). Only write_file/edit_file ever reach this
                # call — read_file/run_command return earlier in this node.
                action=target_step.action,
            )
            response_cache.store(cache_key, content, cache_paths)
        raw_edits = _parse_search_replace_blocks(content)
    except Exception as exc:  # noqa: BLE001 — a generation failure becomes a soft error
        logger.warning("CoderAgent: generation failed on step #%s: %s", step_id, exc)
        fail: Dict[str, Any] = {
            "mission_spec": _mark_step_status(
                mission_spec, target_step.step_number, "failed"
            ),
            "errors": [f"CoderAgent step #{target_step.step_number}: generation failed: {exc}"],
            "current_step_id": target_step.step_number,
            "target_role": target_step.target_role,
        }
        if new_security_flags:
            fail["security_flags"] = new_security_flags
        return fail

    # 3. Validate + apply to an in-memory copy → compute per-file unified diffs.
    from tools.patch_tool import AtomicPatchInput, apply_patch_to_vfs

    local: dict[str, str] = {}
    originals: dict[str, str] = {}
    errors: list[str] = []
    # In-run edits from prior WBS steps (the multi-step loop). A later step editing
    # the same file must anchor its SEARCH blocks to the LATEST in-run version, or
    # sequential same-file edits would each read the original and clobber each other
    # under the operator.or_ reducer. Kept separate from `originals` so the stored
    # diff + base_hash stay anchored to the TRUE committed file (the write pipeline's
    # optimistic-concurrency stale-guard checks disk against base_hash).
    _prior_contents: dict[str, str] = dict(state.get("pending_contents") or {})

    def _read(p: str) -> str:
        if p in local:
            return local[p]
        vfs = _read_vfs(p)
        vfs = vfs if vfs is not None else ""
        originals.setdefault(p, vfs)          # diff + base_hash anchor = true VFS
        prior = _prior_contents.get(p)         # latest in-run edit for this file
        return prior if prior is not None else vfs

    def _write(p: str, c: str) -> None:
        local[p] = c

    for raw_edit in raw_edits if isinstance(raw_edits, list) else []:
        if not isinstance(raw_edit, dict):
            errors.append("CoderAgent: malformed edit skipped.")
            continue
        fp = str(raw_edit.get("file_path", "")).strip()
        sb = str(raw_edit.get("search_block", "") or "")
        rb = str(raw_edit.get("replace_block", "") or "")
        if not fp:
            errors.append("CoderAgent: edit missing file_path skipped.")
            continue
        # New-file / full-content write: empty (or too-short) anchor.
        if len(sb.strip()) < 10:
            originals.setdefault(fp, _read(fp))
            _write(fp, rb)
            continue
        try:
            AtomicPatchInput(file_path=fp, search_block=sb, replace_block=rb)
            apply_patch_to_vfs(_read, _write, fp, sb, rb)
        except Exception as exc:  # noqa: BLE001 — PatchError / ValidationError / syntax
            errors.append(f"CoderAgent: edit to {fp} failed: {exc}")

    patches: dict[str, str] = {}
    contents: dict[str, str] = {}
    base_hash: dict[str, str] = {}
    for p, final in local.items():
        orig = originals.get(p, "")
        diff = "".join(
            difflib.unified_diff(
                orig.splitlines(keepends=True),
                final.splitlines(keepends=True),
                fromfile=f"a/{p}", tofile=f"b/{p}",
            )
        )
        if diff:
            patches[p] = diff
            contents[p] = final           # full new content for the write pipeline
            base_hash[p] = content_hash(orig)  # pre-edit anchor for the stale guard

    # 4. Generated, not yet approved — notify the IDE (non-blocking). 13.0.9:
    # this used to announce "completed" here, before the human ever saw the
    # diff, let alone approved it — the checklist was lying. The apply gate
    # (brain/apply_gate.py) owns every transition from here on: awaiting_approval
    # while a HITL decision is pending, then completed/rejected/revision_requested
    # once the human actually decides, or failed if apply-time validation finds
    # nothing to apply (the orphaned-in_progress backstop).
    _t = asyncio.create_task(
        vfs_manager.emit_graph_mutation(
            session_id=session_id,
            step_number=target_step.step_number,
            new_status="in_progress",
            agent_name="CoderAgent",
        )
    )
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)

    # Fire-and-forget structured explanation of this patch set, rendered beside the
    # diff-approval UI. Best-effort side channel — never gates the graph's control flow.
    from brain.coder_companion import schedule_coder_companion
    schedule_coder_companion(state, attempt_ordinal=state.get("retry_count", 0))

    result: Dict[str, Any] = {
        "mission_spec": _mark_step_status(
            mission_spec, target_step.step_number, "in_progress"
        ),
        "pending_patches": patches,
        "pending_contents": contents,
        "pending_base_hash": base_hash,
        # Which of the (potentially many, cross-step-accumulated) pending_*
        # entries belong to THIS step — pending_patches/contents/base_hash use
        # operator.or_ and can never be cleared, so the apply gate cannot infer
        # "this step's files" from them alone once a later step has touched the
        # same path.
        "pending_step_files": {str(target_step.step_number): list(patches.keys())},
        "current_step_id": target_step.step_number,
        "target_role": target_step.target_role,
        "current_cost_usd": 0.0,
        # Consumed above (matched by step_number) or discarded as stale —
        # either way this node's own turn is the last one that may ever see it.
        "apply_feedback": None,
    }
    if new_security_flags:
        result["security_flags"] = new_security_flags
    if errors:
        result["errors"] = errors
    logger.info(
        "CoderAgent: step #%d produced %d patch(es), %d error(s).",
        target_step.step_number, len(patches), len(errors),
    )
    return result
