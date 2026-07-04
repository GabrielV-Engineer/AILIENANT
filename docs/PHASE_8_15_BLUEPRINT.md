# Division 8.15 Blueprint — Dynamic Subagent Dispatch

**Status:** Ratified — binding (Division 8.15 pending, queued after 8.14).
**Evaluated against:** LangChain's "Dynamic Subagents in Deep Agents" (`deepagents` package, QuickJS code-interpreter middleware + `task()` global).
**Decision:** Do **not** adopt `deepagents` or a JS sandboxed interpreter. Build a native, Pydantic-validated dispatch layer that generalizes existing primitives instead.

**Amendments (applied at 8.15.0 implementation):**
- The SCHEMA_EVOLUTION record for the dispatch schema (§2) lands as **§30**, not the reserved "§27" — §27–§29 were consumed by Division 8.14 after this blueprint was ratified. Purely a numbering reconciliation.
- `SubagentResultEnvelope.raw_digest`'s cap is single-sourced as `shared.config.MAX_OBSERVATION_CHARS` (with `core/tool_dispatch._MAX_OBSERVATION_CHARS` aliasing it), rather than importing the private constant from `core.tool_dispatch` — that module pulls in `core.permissions` + `langchain_core`, which would break `subagent_contracts.py`'s "independently-testable leaf module" intent. Anti-drift guarantee preserved.

**Amendments (applied at 8.15.4 implementation):**
- `brain/dispatch_ledger.py` is **state-channel + synchronous**, not the file-backed ledger §5 implies. Per-task spend is already the authoritative `current_cost_usd` (`operator.add`) + `max_budget_usd` channels (`brain/state.py`), persisted by the checkpointer and gated by `brain/finops.py`; a second file-backed ledger would double-book the same spend and desync. Reservation is single-flight at wave boundaries (one node reserves before fan-out, one reconciles at synthesis), so there is nothing to `FileLock` — only gateway's floor-at-zero-refund *discipline* and the reserve→commit→refund *sequencing* are reused. The functions are pure arithmetic returning deltas the caller folds into `current_cost_usd`; the blueprint's `async def` was for file I/O that no longer exists.
- **Scope split:** 8.15.4 ships the ledger primitives + `subagent_worker`'s real `cost_usd`. The node/edge admission wiring (reserve at `dispatch_origin`, commit at `dispatch_synthesize`, deny→`budget_exhausted` via `dispatch_router`, the `_dispatch_reserved_usd` channel) folds into **8.15.5**, next to the depth/width `status="denied"` re-check — all admission/denial paths land together. The concurrency cap itself was already delivered by 8.15.2's wave-splitting.
- Cost is estimate-based and lenient (DEBT-105): the reserve estimate under-models output/context-growth and the committed actual omits the `answer_fn` synthesis call; `finops`/`check_governor` remain the hard ceiling.

**Amendments (applied at 8.15.5 implementation):**
- The shipped primitives assumed **one dispatch per graph run and a strictly-terminal synthesis** (§3, §6). FULL-scope wiring breaks both: the graph has two dispatch origins (`researcher_agent`, `planner_agent`) that can each open a dispatch in one turn, and the adversarial/loop patterns need more than one round. Two corrections: (a) a **consume-watermark** `_dispatch_consumed` — because `_dispatch_results` is `operator.add` and cannot be cleared, synthesis digests/commits only `results[consumed:]` and advances the watermark, isolating a second dispatch from the first's envelopes/cost; (b) a **`dispatch_round_count` + new `dispatch_advance` node** separating pattern rounds (a *new* task set: critic, continuation) from wave-splitting (slicing one fixed task list). Synthesis stays terminal *per top-level dispatch* and additionally clears `dispatch_plan=None` so a stale plan cannot re-fire an origin edge (§32).
- `dispatch_origin` is no longer a pass-through — it is the **admission gate** (validate → depth/width deny → single-flight budget reserve). Admission routes by a **pure string router** (`route_after_admission` → `dispatch_fanout` | `dispatch_synthesize`); the Send-only fan-out lives on the new `dispatch_fanout` node so no conditional edge ever mixes a `str` node key with a `list[Send]` return. Denial-envelope generation is bounded to `MAX_DISPATCH_WIDTH` (an over-scoped raw plan fails Pydantic validation, then materializes at most `MAX_DISPATCH_WIDTH` rejection envelopes — never one per raw task, which would block the event loop / overflow the checkpoint).
- Return routing uses an additive `dispatch_return_node` channel (planner → `drift_compute`, researcher → `planner_agent`), so one shared synthesize node rejoins the correct successor.
- The `analyst_readonly` floor-lock is **automatic** via `core/permissions.py::evaluate_action` (any identity not granted `EDIT_EXECUTE_RBW` is capped at READ_ONLY) — the role→identity binding lives in `shared/rbac.py` (`resolve_dispatch_permission`), not `core/permissions.py` as the blueprint anticipated; the worker resolves per-role `agent_permission` there.
- **MVP declarations (DEBT-106/107):** the 8 dev roles resolve `EDIT_EXECUTE_RBW` but run tool-less — no static `RegisteredTool` arsenal builder exists for them (dev tooling is runtime-MCP-registered), so per-role executable arsenals are deferred (DEBT-106). Autonomous LLM plan-emission is deferred: `brain/dispatch_emitter.py` ships the full mechanism (injected hook + `AILIENANT_DISPATCH_DEBUG` synthetic + counter resets + graph wiring), but the production prompt that decides when to fan out is future work (DEBT-107). Tournament winner-selection uses a lightweight score/ok selector, not the full MCTS+verify `run_tournament_from_dispatch` (DEBT-104 stays warn-only).

**Amendments (applied at 8.15.3 implementation):**
- The §4 "verbatim relocation" moves not only `select_candidate_via_mcts` (as `run_tournament`) but also its private helpers `_verdict_reward`/`_vfs_to_view`/`_content_to_vfs`. `_content_to_vfs` is still needed by `run_agentic_cell_node`, so `agentic_cell` re-imports it from `brain/subagent_tournament.py` — keeping the dependency edge unidirectional (`agentic_cell → subagent_tournament`) and single-sourcing the helper. `_verdict_reward`/`_vfs_to_view` are used only by the moved body and are not re-imported.
- `run_tournament_from_dispatch` gains an optional `candidate_extractor` parameter (additive to the §4 signature — all named params retained). The envelope→`{path:content}` mapping is genuinely undefined until the six-pattern wiring (§7 / 8.15.5): `structured_result` is keyed by arbitrary response-schema field names, not paths. The default extractor treats string-valued fields as `path→content` (a convention that only holds for path-keyed schemas); real patterns supply an extractor rather than force a behavior-changing edit to the shipped function.
- Isolation caveat (DEBT-104): `run_tournament`'s surface rollback (`push_vfs_to_surface`) only writes, never deletes, so a candidate's newly introduced paths survive the roll-back. Benign for the agentic cell (same-file candidates) but a contamination risk for heterogeneous dispatch candidates; the adapter warns (does not reject), and full delete-not-in-base isolation is deferred to §5/8.15.5 wiring.

**Amendments (applied at 8.15.1/8.15.2 implementation):**
- `_dispatch_results` is **never cleared mid-dispatch**. §2/§3 describe `dispatch_synthesize` as clearing it to `[]`, but an `operator.add` channel cannot be reset (returning `[]` concatenates to a no-op). Rather than add a custom reset-sentinel reducer, `dispatch_synthesize` is made **terminal** (runs once after the final wave, digests all accumulated results) and the wave loop-back lives in `route_after_workers` on a dedicated fan-in node `dispatch_gate`. This keeps R6's `operator.add`-from-day-one and bounds peak concurrency at the cap; the raw channel is simply superseded per dispatch invocation.
- The wave decision hangs off the single fan-in node `dispatch_gate` (plain edge from `subagent_worker`), **not** a conditional edge on `subagent_worker` itself — a conditional edge on a fanned-out node fires once per Send instance (verified against `route_after_coder`), which would re-fan N× per wave.
- Per the current tool infrastructure, only `analyst_readonly` has an executable `Dict[str, RegisteredTool]` map (`build_analyst_tools`); the 8 dev roles run tool-less (pure-reasoning subagents) until their arsenals land in **8.15.5**.

---

## 1. Rationale

LangChain's Dynamic Subagents feature lets an LLM write a short JavaScript program, executed inside a
sandboxed QuickJS interpreter, that calls a `task(description, subagentType, responseSchema)` global to
fan out many subagent LLM calls programmatically — instead of one sequential tool call per subagent.
Confirmed via LangChain's own docs/DeepWiki: `create_deep_agent()` still compiles to a real LangGraph
`CompiledStateGraph` (a middleware stack — todo-list, filesystem, subagent, code-interpreter — on top of
LangGraph, not a rival orchestration paradigm). Six canonical patterns emerge: classify-and-act,
fanout-and-synthesize, adversarial-verification, generate-and-filter, tournament, loop-until-done.

Three reasons this repo does **not** adopt the package or its interpreter wholesale:

1. **Architecture fork.** `deepagents`'s middleware stack (todo tools, filesystem tools, subagent
   middleware) duplicates capability AILIENANT already owns natively and more strictly:
   `core/task_service.py` (task lifecycle + DLQ), `core/permissions.py` (7×3 permission matrix),
   the VFS middleware layer (Charter §0.1). Adopting the package would mean running two competing
   task/permission substrates side by side.
2. **New arbitrary-code-execution trust boundary.** A JS interpreter executing LLM-authored code is a
   new Gateway-class boundary (Charter §3: "Gateway / MCP / Transport — the untrusted boundary... treat
   every inbound payload as hostile"). Even sandboxed (QuickJS/WASI), this is a materially larger attack
   surface than validating a structured, closed-schema data object — and the codebase's existing hardest
   isolation tier (`WasmSandboxAdapter`, `core/sandbox.py`) is reserved for *shell command* execution, not
   arbitrary LLM-authored program logic with callback re-entry into the host's own agent-dispatch surface.
3. **Dependency-governance precedent.** Charter §9 rejected `scipy` in favor of a hand-rolled
   `degree_centrality` specifically to avoid a heavyweight transitive tree for a narrow surface area. A
   JS engine binding (QuickJS via Python bindings, or Node-specific `vm2`/`isolated-vm`) is a materially
   heavier, less portable dependency (Windows/POSIX portability mandate, Charter §5.6) for a capability
   this repo can already approximate with primitives it owns outright.

**What is actually novel and worth taking**, independent of the JS-interpreter mechanism: (a) deterministic,
structural fan-out coverage over N items instead of prompt-engineered thoroughness; (b) keeping raw
subagent output out of the parent's context window until synthesized; (c) named orchestration shapes
(fanout-and-synthesize, tournament, generate-and-filter, adversarial-verification, loop-until-done,
classify-and-act) as reusable primitives rather than ad hoc prompting. All five are achievable by
generalizing what already exists in this codebase:

| Existing primitive | File | Generalizes into |
|---|---|---|
| `Send()`-based fan-out (SWARM mode) | `brain/engine.py::route_to_coders` (line ~259) | `brain/dispatch.py::build_dispatch_sends()` |
| Contained MCTS tournament (candidate edits) | `brain/agentic_cell.py::select_candidate_via_mcts` (line ~370) | `brain/subagent_tournament.py::run_tournament()` |
| Reserve/refund budget ledger | `gateway/ledger.py` | `brain/dispatch_ledger.py` |
| Observation truncation ceiling | `core/tool_dispatch.py::_MAX_OBSERVATION_CHARS` | reused directly (imported, not re-defined) |
| Tier-based token ceilings | `core/memory/graphrag_extractor.py` | per-batch digest ceiling in `dispatch_synthesize` |
| Role-tier permission floor (Researcher → READ_ONLY always) | `core/permissions.py` | new `analyst_readonly` subagent-role floor |

No new third-party dependency is required. No `eval`/`exec` of LLM-authored code is introduced anywhere.

---

## 2. Contracts

New file: `ailienant-core/brain/subagent_contracts.py`. Kept separate from `brain/state.py` (which only
gets the four thin channel additions) the same way `brain/mcts/tree.py` is a dedicated module rather than
inline in `agentic_cell.py` — a shared, independently-testable capability gets its own module.

```python
from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class SubagentResponseField(BaseModel):
    """One field in a subagent's expected structured return shape."""
    name: str
    type: Literal["str", "int", "float", "bool", "list_str"]
    description: str

class SubagentResponseSchema(BaseModel):
    """Closed field-type vocabulary — deliberately NOT a free-form JSON-schema dict
    (avoid the mcp_adapter._McpToolInput anti-pattern); keeps validation and
    per-result truncation tractable."""
    fields: List[SubagentResponseField] = Field(min_length=1, max_length=8)

class SubagentTask(BaseModel):
    """One unit of dispatch work — the structured analog of a task(description,
    subagentType, responseSchema) call. No session_permission_mode field: a
    subagent cannot request a more permissive mode than its parent turn (§7.3)."""
    task_id: str                      # uuid4 hex, caller-assigned, idempotency key
    description: str = Field(max_length=4000)
    subagent_role: Literal[
        "core_dev", "architect_refactor", "devops_infra", "secops",
        "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer",
        "analyst_readonly",   # verification/critic role, READ_ONLY tier only — §7.3
    ]
    response_schema: SubagentResponseSchema
    context_refs: List[str] = Field(default_factory=list, max_length=20)  # VFS paths, never raw content
    max_iterations: int = Field(default=1, ge=1, le=8)

class DispatchPlan(BaseModel):
    """Structured, schema-validated fan-out plan — replaces "LLM writes an
    executable script" with "LLM emits data validated by Pydantic"."""
    pattern: Literal[
        "classify_and_act", "fanout_and_synthesize", "adversarial_verification",
        "generate_and_filter", "tournament", "loop_until_done",
    ]
    tasks: List[SubagentTask] = Field(min_length=1, max_length=32)  # hard fan-out width ceiling, §6
    synthesis_instruction: str = Field(max_length=2000)
    dispatch_depth: int = Field(default=0, ge=0, le=2)  # recursion ceiling, §6

class SubagentResultEnvelope(BaseModel):
    """Result of ONE subagent call. raw_digest is ALWAYS pre-truncated (§4) before
    construction — never a full transcript."""
    task_id: str
    status: Literal["ok", "error", "budget_exhausted", "denied"]
    structured_result: Optional[Dict[str, Any]] = None  # validated against response_schema
    raw_digest: str = Field(max_length=4000)             # reuses core.tool_dispatch._MAX_OBSERVATION_CHARS
    cost_usd: float = 0.0
    iterations_used: int = 0
    error_message: Optional[str] = None

class DispatchBatchResult(BaseModel):
    """Aggregate of one dispatch — this, not N raw transcripts, folds into
    AIlienantGraphState."""
    batch_id: str
    pattern: str
    results: List[SubagentResultEnvelope]
    total_cost_usd: float
    winner_task_id: Optional[str] = None  # populated for tournament/generate_and_filter
```

**`AIlienantGraphState` channel additions** (`brain/state.py`, additive-only, all default-safe so a
pre-8.15 checkpoint deserializes unchanged):

```python
dispatch_plan: Optional[Dict[str, Any]]           # DispatchPlan.model_dump(), written by the dispatching node
dispatch_batch_result: Optional[Dict[str, Any]]   # DispatchBatchResult.model_dump(), written by dispatch_synthesize
dispatch_depth: int                               # default 0; incremented before any re-dispatch
subagent_dispatch_trace: Annotated[List[Dict[str, Any]], operator.add]  # append-only audit trail
```

Plus one internal (non-channel, `Send()`-payload-only) key: `_dispatch_task` (the per-task slice a
`subagent_worker` invocation reads) and one reducer-guarded fan-in channel:
`_dispatch_results: Annotated[List[Dict[str, Any]], operator.add]` — cleared to `[]` by
`dispatch_synthesize` in the same delta that writes `dispatch_batch_result`.

**`SCHEMA_EVOLUTION.MD §27`** (to be written at 8.15.0 implementation time — reserved here so the number
doesn't collide; current max section is `§26`): document `brain/subagent_contracts.py` as new, the four
state channels as additive/scalar-overwrite with safe defaults, and state explicitly that no existing
field is renamed or narrowed — consumers written before 8.15 ignore the new fields entirely.

---

## 3. Dispatch mechanics

New file: `ailienant-core/brain/dispatch.py`. **Not** added to `brain/swarms.py` — that module is
SWARM/RELAY-topology-specific; the general capability gets its own home so it never shares a call site
with `route_to_coders`.

```python
from __future__ import annotations
from typing import Any, Mapping
from langgraph.constants import Send
from brain.subagent_contracts import DispatchPlan

def build_dispatch_sends(
    plan: DispatchPlan,
    base_state: Mapping[str, Any],
    *,
    target_node: str = "subagent_worker",
) -> list[Send]:
    """Structured analog of route_to_coders()'s Send()-fanout: one Send per
    SubagentTask. dispatch_depth is incremented here (never left to the worker)
    so every fan-out edge enforces the recursion ceiling (§6), independent of
    the Pydantic-layer bound already on DispatchPlan.dispatch_depth."""
```

`route_to_coders()` (`brain/engine.py:259`) is **read but never edited** — `build_dispatch_sends` reuses
its `Send()`-payload-augmentation idiom (`{**state, "active_role": ..., "current_step_id": ...}`) as a
pattern, not as shared code. This is the regression-risk mitigation for R1 (§8): zero code path exists by
which shipping this feature can change SWARM/RELAY behavior, because the two functions never call into
each other and never share a state-mutation site.

**New node `subagent_worker`** (`brain/nodes/subagent_worker_node.py`) — thin, narrow-contract node (NOT a
reuse of `run_coder_node`, since a dispatched subagent must return a `SubagentResultEnvelope`, not a VFS
patch):
1. Reads its own `_dispatch_task` slice.
2. Resolves the role's tool subset via the existing `McpToolRegistry.bind_tools(llm, AgentRole)` pattern.
3. Runs 1..`max_iterations` turns via the existing `core/tool_dispatch.py::ToolDispatcher` loop (already
   has `_MAX_OBSERVATION_CHARS` truncation and `evaluate_action` gating built in — no second dispatch loop
   is invented).
4. Validates the final answer against `response_schema` field-by-field (explicit, auditable checks —
   deliberately not `pydantic.create_model` metaprogramming, per the charter's preference for explicit
   over clever).
5. Writes its `SubagentResultEnvelope` into `_dispatch_results` (reducer-guarded fan-in, mirrors
   `_merge_generated_code`'s collision safety for concurrent `Send()` writers).

**New node `dispatch_synthesize`** runs after all fanned-out `Send()`s rejoin at the next LangGraph
super-step (the same native barrier `FULL_SWARM`'s `micro_swarm → analyst_agent` edge already relies on):
collects `_dispatch_results`, applies context-isolation digesting (§4), writes one `DispatchBatchResult`
to `dispatch_batch_result`, clears `_dispatch_results` to `[]`.

**New, additive conditional edges**: `planner_agent`/`researcher_agent` each get a *new* conditional edge
into `subagent_worker`, wrapping their existing target so that when `dispatch_plan` is `None` (the
default), the router returns exactly the pre-8.15 target — the existing unconditional/conditional edges
are never deleted or rewritten, only wrapped. See §8 R3/R4 for the feature-flag and topology-identity
guarantee.

---

## 4. Tournament reuse

New file: `ailienant-core/brain/subagent_tournament.py`. Extraction strategy — **verbatim relocation plus
re-export shim**, not a rewrite:

1. Move `select_candidate_via_mcts`'s body into `subagent_tournament.py` as `run_tournament()` with an
   **identical signature** (`surface, clean_base_content, candidates, verify_command, run_verify,
   blob_store, mission_state`).
2. `agentic_cell.py` keeps the name alive as a one-line re-export:
   `from brain.subagent_tournament import run_tournament as select_candidate_via_mcts`.
   Every existing call site (`run_agentic_cell_node`'s candidate-edit loop) and every existing test
   (`test_phase7_19_2_agentic_cell.py`, notably `test_mcts_branch_selects_best_verdict` and
   `test_mcts_rolls_back_surface_between_candidates`) keeps working against the same import name with
   byte-identical behavior.
3. New entry point, same module: `run_tournament_from_dispatch(batch_result, *, surface,
   clean_base_content, verify_command, run_verify, blob_store, mission_state)` — adapts a
   `DispatchBatchResult` (N subagent-proposed patches) into the same `candidates: List[Dict[str,str]]`
   shape `run_tournament` already expects, then delegates. No duplication of the transactional
   push→verify→rollback logic or of `brain/mcts/tree.py`'s `MCTSNode`/`MCTSTree`/UCB1 selection — both are
   imported/used exactly as they are today.

This is the reuse path for the **generate-and-filter** and **tournament** canonical patterns (§6): N
subagents each propose a candidate (`generate`), `run_tournament_from_dispatch` runs the identical
score→rollback→UCB1-select loop already proven safe inside the agentic cell (`filter`).

---

## 5. Budget & concurrency

New file: `ailienant-core/brain/dispatch_ledger.py`. Reuses `gateway/ledger.py`'s **mechanics** — `FileLock`
+ atomic write + floor-at-zero refund — as a pattern, not as a shared import, because the two ledgers
sit on opposite sides of the Charter §3 Gateway boundary: `gateway/ledger.py` polices *external* MCP
callers against `AILIENANT_GATEWAY_BUDGET`; a dispatch batch is an *internal* graph-to-graph operation
that must be checked against the **per-task** `max_budget_usd`/`current_cost_usd` channels the rest of
the graph already respects. Blurring the two would let internal dispatch escape the task's own budget
ceiling or contaminate the Gateway's external-caller keyspace with internal `batch_id`s.

```python
async def reserve_dispatch_budget(
    *, task_id: str, batch_id: str, estimated_cost_usd: float,
    current_cost_usd: float, max_budget_usd: float,
) -> "DispatchReservation | None":
    """Atomically reserve estimated_cost_usd against the task's remaining budget.
    Returns None (deny, fail-closed) if the reservation would exceed max_budget_usd."""

async def commit_dispatch_actual(reservation, actual_cost_usd: float) -> float:
    """Reconcile reserved vs actual; returns the refund delta (>= 0). Never lets
    a refund go negative — mirrors gateway/ledger.py's floor-at-zero discipline."""

async def refund_dispatch_reservation(reservation) -> None:
    """Full refund on total batch failure (prepare-then-commit-or-compensate,
    Charter §5.4)."""
```

Cost estimation reuses `brain/iteration_governor.py::estimate_iteration_cost()` (one formula —
`C_in·T_in + C_out·T_out` — summed across the planned tasks) rather than inventing a second cost model.

**Concurrency cap**: not a runtime `asyncio.Semaphore` fighting LangGraph's native `Send()` parallelism.
Instead, `AILIENANT_MAX_CONCURRENT_SUBAGENTS` (default 4, same env-configurable/floor-at-1 pattern as
`core/benchmark_service.py::_max_concurrent()`) is enforced **at plan-construction time**: if
`len(plan.tasks)` exceeds the cap, `dispatch_synthesize` splits the plan into sequential waves via a
loop-back edge (the same shape `agentic_cell`'s `route_after_cell` "continue" loop-back already uses).

**Reserve→commit→refund sequencing** (binding, Charter §5.4 — prepare everything before committing, or
explicit compensation):
1. Before `build_dispatch_sends()` is called for a wave, reserve the wave's estimated cost. On deny,
   skip the fan-out entirely and route straight to synthesis with `status="budget_exhausted"` envelopes
   for every task in that wave — never silently drop the plan.
2. On success, fan out; `dispatch_synthesize` sums actual costs and commits, refunding the
   reserved-minus-actual delta.
3. If wave preparation itself fails before any subagent runs, fully refund the reservation.

---

## 6. Context isolation

Two ceilings, both reused rather than reinvented:

1. **Per-result**: `SubagentResultEnvelope.raw_digest` is capped at `core/tool_dispatch.py`'s existing
   `_MAX_OBSERVATION_CHARS` (4000) — imported directly so the two ceilings can never drift apart.
   Truncation happens inside `subagent_worker`, before the envelope is constructed — the raw transcript
   never reaches even the reducer-merged `_dispatch_results` channel.
2. **Per-batch**: `dispatch_synthesize` applies a second GraphRAG-style digest
   (`core/memory/graphrag_extractor.py`'s tier-ceiling pattern — `LOCAL_SMALL`/`LOCAL_BIG`/`CLOUD`), keyed
   off the **parent's** `active_llm_profile`, not a fixed constant: a 20-envelope batch folding back into
   a `LOCAL_SMALL`-routed parent turn is truncated harder than one folding into a `CLOUD`-routed turn.
   Greedy-pack under budget (winner-first for tournament/generate-and-filter, declaration order
   otherwise), same whole-chunk-only discipline as `agents/analyst_context.py`'s `ContextBudgetManager`.

Only `dispatch_batch_result` (already digested) ever lands in `AIlienantGraphState` for a synthesis
prompt to read — `_dispatch_results` (the raw per-worker list) is cleared in the same delta that writes
it, mirroring how `agentic_cell`'s working/`clean_base_content` locals never themselves become state.

---

## 7. Pattern mapping

| Pattern | AILIENANT primitive(s) |
|---|---|
| **classify-and-act** | `DispatchPlan(pattern="classify_and_act", tasks=[single_task])` — the caller's own LLM classifies, then dispatches one task; the degenerate N=1 case of `build_dispatch_sends`, no new primitive. |
| **fanout-and-synthesize** | `build_dispatch_sends()` (§3) fans N tasks in one wave; `dispatch_synthesize` (§3) collects + digests (§6) into `DispatchBatchResult`. The primary new primitive. |
| **adversarial-verification** | Two sequential waves: wave 1 = producer subagent(s); wave 2 = a single `subagent_role="analyst_readonly"` critic task whose `response_schema` demands a verdict field. Sequenced via `dispatch_synthesize`'s loop-back edge. Critic is READ_ONLY-tier only (§7.3) so it cannot mutate what it is judging. |
| **generate-and-filter** | `run_tournament_from_dispatch()` (§4) — N subagents `generate`, existing MCTS/UCB1 `filter`s. |
| **tournament** | Same as generate-and-filter; `MCTSTree.select_best_child` is already N-ary, no bracket logic needed. |
| **loop-until-done** | `dispatch_synthesize`'s conditional loop-back edge (mirrors `route_after_cell`'s "continue" status), bounded by a new `dispatch_wave_count` scalar channel checked against `brain/iteration_governor.py::check_governor()`'s existing 3-axis (steps/tokens/time) ceiling — reused directly, not reimplemented. |

---

## 8. Regression risks and mitigations

| Risk | Where | Mitigation |
|---|---|---|
| **R1 — SWARM/RELAY behavior change** | `brain/engine.py::route_to_coders` | Zero edits to `route_to_coders` or its call site (`workflow.add_conditional_edges("drift_gate", route_to_coders, ...)`). `build_dispatch_sends` is a separate function in a separate module. CI gate: `tests/test_swarms.py` unmodified and green. |
| **R2 — MCTS tournament behavior change** | `brain/agentic_cell.py::select_candidate_via_mcts` | Verbatim-body relocation + re-export shim, not a rewrite. CI gate: `test_phase7_19_2_agentic_cell.py` (incl. the two MCTS-specific tests), `test_mcts_daemon.py`, `test_mcts_mirror.py` unmodified and green. |
| **R3 — `engine.py` graph-wiring breakage** | New conditional edges from `planner_agent`/`researcher_agent` | New edges wrap, never replace, existing targets; when `dispatch_plan is None` (default) the wrapping router returns the pre-8.15 target. CI gate: `test_full_swarm.py`, `test_micro_swarm.py`, `test_micro_swarm_e2e.py` green. |
| **R4 — Unbounded rollout risk** | Whole feature | `AILIENANT_ENABLE_DYNAMIC_DISPATCH` (default off) checked once at graph-*construction* time in `engine.py` — when off, the new nodes/edges are never added to the `StateGraph`, so a disabled deployment's compiled graph has the exact node/edge set it has today, not just a runtime no-op. |
| **R5 — Checkpoint deserialization of pre-8.15 runs** | `HybridCheckpointer` / SQLite WAL | All four new channels default to `None`/`0`/`[]` (§2). New test: `test_dispatch_channels_default_on_legacy_checkpoint` in the 8.15.6 gate. |
| **R6 — Reducer collision under fan-out** | New `_dispatch_results` channel | Ships with an `operator.add` reducer from day one (never as a bare list) — the same class of bug `_resolve_step_id`/`_resolve_target_role`/`_merge_generated_code` were retrofitted to fix for SWARM. Write the "two concurrent Sends write in the same super-step" test before the node lands, not after. |

---

## 9. Security considerations

**9.1 — Recursion / fan-out exhaustion.** A `DispatchPlan` could in principle request subagents that each
request further fan-outs. Mitigated in depth:
- Pydantic-layer cap: `DispatchPlan.dispatch_depth` bound `[0,2]`, `tasks` bound `[1,32]` — fails fast on
  an over-scoped plan.
- Code-layer re-check: `build_dispatch_sends` independently verifies
  `base_state.get("dispatch_depth", 0) >= MAX_DISPATCH_DEPTH` and **denies** (not silently truncates) —
  defense in depth in case a future caller ever constructs a `DispatchPlan` without LLM/schema validation.
- Named ceiling on the product: depth 2 × width 32 → worst case 1024 total subagent invocations for one
  root dispatch. This product (`MAX_TOTAL_DISPATCH_FANOUT`) must be a named constant asserted in the
  8.15.6 checkpoint gate so a future edit to either cap can't silently blow past a reviewed ceiling.
- Budget as backstop: even if depth/width caps were bypassed, `dispatch_ledger.py`'s reservation against
  the finite per-task `max_budget_usd` fails the batch closed on exhaustion — the same "budget is the
  backstop even when logic caps fail" posture `iteration_governor.py` already provides single-agent.

**9.2 — Prompt-injection smuggling via `response_schema`.** A structured schema narrows shape, not
content — a string-typed field could still carry injected instructions a downstream synthesis call might
misread as commands. Mitigations: every `raw_digest`/string field is rendered as **data**, never spliced
as a `system`-role message (same delimited-boundary discipline `engine.py::resolve_explicit_mentions`
already applies to `@-mention` file content); the existing `SecretsScrubber.scrub()`
(`agentic_cell.py::audit_tool_args`) is applied to every `raw_digest` and string field before it folds
into `DispatchBatchResult`; `subagent_dispatch_trace` records every dispatched task's role/pattern/status
for forensic visibility, mirroring `permission_audit_log`/`tool_dispatch_trace`.

**9.3 — Permission-matrix interaction.** No new permission axis, no new enum on `evaluate_action`. A
dispatched subagent resolves tools via the existing `evaluate_action(session_mode, tool_tier,
agent_permission)` matrix and `McpToolRegistry.bind_tools(llm, AgentRole)` exactly as an ordinary WBS step
would. `SubagentTask` has no `session_permission_mode` field, and `build_dispatch_sends` copies that value
only from the parent's own `base_state` — there is no wire path for a subagent to request a more
permissive mode than its parent turn is already running under. The new `analyst_readonly` role (the
adversarial-verification critic) must be floor-locked to `ToolPrivilegeTier.READ_ONLY` regardless of
session mode — `core/permissions.py` already establishes this exact precedent for the Researcher role
("Researcher / routing-only can never exceed the READ_ONLY tier, in any session mode"); the 8.15.5
implementer must re-verify the current role→tier binding call site before wiring `analyst_readonly`, since
permissions code may shift before this sub-phase is reached.

**9.4 — No new arbitrary-code-execution surface.** By design: no JS/QuickJS interpreter, no `eval`/`exec`
of LLM-authored code anywhere in this feature. Every LLM output that drives dispatch is a
Pydantic-validated structured object (`DispatchPlan`, `SubagentTask`), parsed exactly as
`MissionSpecification`/`WBSStep` already are — fail-fast on malformation, no code-execution path for a
malicious or hallucinated dispatch plan to reach.

---

## Critical files

- `ailienant-core/brain/subagent_contracts.py` (new) — `DispatchPlan`, `SubagentTask`,
  `SubagentResponseSchema`, `SubagentResultEnvelope`, `DispatchBatchResult`
- `ailienant-core/brain/dispatch.py` (new) — `build_dispatch_sends()`
- `ailienant-core/brain/subagent_tournament.py` (new) — `run_tournament()` (relocated),
  `run_tournament_from_dispatch()`
- `ailienant-core/brain/dispatch_ledger.py` (new) — `reserve_dispatch_budget()` /
  `commit_dispatch_actual()` / `refund_dispatch_reservation()`
- `ailienant-core/brain/nodes/subagent_worker_node.py`, `dispatch_synthesize_node.py` (new)
- `ailienant-core/brain/agentic_cell.py` — becomes a one-line re-export shim for
  `select_candidate_via_mcts`; zero other changes
- `ailienant-core/brain/engine.py` — new additive/opt-in conditional edges, feature-flagged at
  graph-construction time; `route_to_coders` itself untouched
- `ailienant-core/brain/state.py` — four new additive channels + `_dispatch_results` reducer
- `ailienant-core/core/tool_dispatch.py` — reused as-is by `subagent_worker`
- `ailienant-core/gateway/ledger.py` — reused as a pattern reference only, not imported
- `docs/SCHEMA_EVOLUTION.MD` — new `§27` entry (at 8.15.0 implementation time)
- `docs/PROJECT_MANIFEST.md` — Division 8.15 WBS (already added)
