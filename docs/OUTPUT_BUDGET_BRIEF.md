# Design Brief — Output Budgeting, Plan Depth, and Completion Integrity

> **Status:** RESOLVED — 2026-08-26 (Manifest 13.1.3, see `docs/DEV_JOURNAL.md`). §6's
> measurements (M1-M9) confirmed the brief's framing on completion integrity, refuted the
> native-thinking mechanism as a factor in Discovery A, and surfaced ten further defects
> (N1-N10) not named below. Both discoveries are closed: A (context capacity) via an explicit
> runtime-probed `num_ctx` and real per-call budget arithmetic; B (completion integrity) via
> the coder's truncated-block guard, AST validation wired into the apply gate before any
> write, and a new `run_checks` node executing a plan's own acceptance criteria. §8.4/§8.1/§8.3
> were evaluated and NOT built (each ruled out by a specific measurement — see the journal
> entry); the remainder of §8 shipped. This document is kept as the historical investigation
> record; the current contracts are `docs/SCHEMA_EVOLUTION.MD` §57 and
> `docs/TECH_DEBT_BACKLOG.md` DEBT-203 through DEBT-208.
> **Created:** 2026-08-25, from a live Plan-mode failure plus a follow-up audit that
> uncovered a second, larger problem behind it.
> **How to use this file:** it is the entry brief for a dedicated work session. Read it in
> full, then execute §6 (Required first pass) BEFORE proposing any implementation. Nothing
> below is a spec — §8 lists *candidate* directions with tradeoffs, and §6 exists precisely
> because the data must decide between them.
> **Scope note:** this brief covers TWO failure modes that share a root-cause family and must
> be fixed together (see §1). Fixing only the loud one leaves the dangerous one intact.

---

## 1. The two discoveries

### Discovery A — the loud failure: a plan that dies with a misleading error

A Plan-mode turn on local hardware failed with:

```
I couldn't draft a plan: Planner Error - schema validation exhausted 3 attempts:
6 validation errors for MissionSpecification
outcome  Field required [type=missing, input_value={}, input_type=dict]
scope    Field required [type=missing, input_value={}, input_type=dict]
... (4 more)
```

`input_value={}` did **not** come from the model. `LLMGateway._extract_nested_schema_target`
(`tools/llm_gateway.py`) collapses *"the model returned nothing parseable"* into an empty
dict, which Pydantic then reports as six missing fields. The schema was never the problem —
the schema was never reached. The model produced an unparseable (almost certainly truncated)
draft, three times.

**Evidence from the live run** (`.ailienant_telemetry.log`, session `1db7de19-…`):

| Observation | Value | Why it matters |
|---|---|---|
| Planner node window | `09:27:04` → `09:39:10` = **726 s**, 3 attempts ≈ **242 s each** | |
| `resolve_local_timeout(4096, model)` for this call | ≈ **2108 s** | 242 s ≪ 2108 s → **the timeout never fired.** NOT DEBT-191 recurring. |
| Context pipeline record | `ratio=0.1440 total_tokens=1180 token_budget=8192` | Prompt was small. The **input** side was never under pressure. |
| `token_budget` | exactly **8192** | The literal value of `DEFAULT_CONTEXT_BUDGET` (`brain/agent_context.py:36`) — see §4.b. |

### Discovery B — the silent failure: work that "succeeds" while being substantively incomplete

Reported independently by the operator, and **more important than Discovery A**:

> *"Solicité crear una landing page completa — estructura, archivos, etc. Creó varios archivos
> pero cada uno tenía muy poco código, básico, no lo suficiente para una página web completa.
> Hace el trabajo a medias."*

Many files, each a thin stub. Every gate passed. The turn reported success. **Nothing in the
pipeline noticed.** §4.f–§4.i explain why, and §4.g is the finding that most changes the
shape of this task.

**These are one problem wearing two masks:** output capacity is under-provisioned and
under-verified. Discovery A is what it looks like when the budget runs out *loudly*;
Discovery B is what it looks like when it runs out *quietly*. Fix them in one pass.

---

## 2. The architectural asymmetry (the core framing)

State this back before proposing anything.

**Input tokens are a *selection* problem.** More material than fits → rank, retrieve,
summarize, evict. GraphRAG, the 5-layer `ContextPipeline`, recency scoring and compaction all
solve this, and solve it well.

**Output tokens are a *generation* problem.** You cannot retrieve, rank, or compress tokens
that do not exist yet. The only real degrees of freedom are **generate less** or **generate in
pieces**. No amount of retrieval fixes a window too small to *write* the answer.

The system asks for **monolithic, atomic, long structured outputs**: an entire
`MissionSpecification` (outcome + scope + constraints + decisions + N `WBSStep`s with prose +
checks) in ONE completion, then one file per step in ONE completion each. Output length scales
with the work; the window is fixed. Nothing bridges that.

**The dangerous corollary — this is Discovery B.** Truncation is the *benign* failure: it
fails loudly with broken JSON. The dangerous failure is one step earlier — a model that *does*
close the JSON, or *does* emit syntactically valid code, but silently shrank the work to fit.
That output passes every gate the system currently has. This is the real hallucination /
under-delivery surface, and today it is completely invisible.

---

## 3. Why compaction is NOT the cause of either failure — and its real state

**Ruled out with data, not theory.** The operator's hypothesis (in-session auto-compaction
truncating context mid-task) was tested directly by running the shipped gate tool against the
live dogfood log:

```
python -m core.benchmark.context_telemetry_report <workspace>/.ailienant_telemetry.log

records: 32 (summarizer=6, pipeline=26)   sessions: 5 (non-trivial=1)
sessions w/ event: 0                      event fraction: 0.000 (bar=0.25, min_sessions=10)
RECOMMENDATION: INSUFFICIENT_DATA
```

Compaction fires at `THRESHOLD_RATIO=0.80`. Observed utilization: **0.09–0.14**. It has
**never fired even once** in real usage. It cannot be the cause of anything. Do not spend the
session there.

Record the accurate state so a future session does not re-derive it:

- **Compaction is live, not missing.** `brain/summarizer.py::StateSummarizer` is a real,
  LLM-backed compaction node (`THRESHOLD_RATIO=0.80`, `KEEP_LAST_N=5`, wired via
  `on_state_compacted`). Not a heuristic stand-in.
- **Its known gap is positional, not semantic.** Per `docs/PHASE_8_16_BLUEPRINT.md`, the
  summarizer prompt *already* instructs preserving architectural decisions and unresolved
  issues. What lacks protection is anything older than `KEEP_LAST_N=5` **by index**. That is
  what 8.16.1 exists to fix.
- **8.16.0 (the division's binding GO/NO-GO gate) is `[x]` DONE** — it shipped `CONTEXT`
  telemetry, a synthetic corpus generator, and the report tool above. 8.16.1–8.16.4 are
  blocked on a **data verdict, not on design**; their manifest specs are already detailed.
- **The blueprint's "no log exists yet (cold start)" is now stale.** Real data has begun
  accruing. Zero events so far is a weak directional lean toward NO-GO, far from conclusive.
  The gate resolves itself at ~10 non-trivial sessions with no new engineering — just keep
  dogfooding and re-run the tool.

**Conceptual link worth carrying (do not merge the tasks):** §2's silent-degradation case is
the *same failure class* as lossy compaction — irrecoverable information dropped under space
pressure with no loud signal. Different code, different task, but whoever designs the
output-side answer should know the project already has a designed philosophy for the
input-side twin and should not invent a contradictory one.

---

## 4. Concrete defects (verified in code)

Confirm each before acting; they are findings, not a blind to-do list.

### 4.a — Input and output budgets are computed independently against the SAME window
- `resolve_context_budget(state)` (`brain/agent_context.py`) returns the model's **entire**
  declared window as the **input** budget (log: `token_budget=8192`, ratio `0.144`).
- `_resolve_planner_draft_max_tokens(user_input, budget)` (`agents/planner.py`) separately
  claims `budget // 2` = 4096 as the **output** ceiling.
- Sum = **12288** against a window of **8192**. **Nothing validates the sum.** It survived
  this run only because the actual prompt was 1180 tokens.
- `agents/coder.py::_resolve_coder_max_tokens` has the identical `budget // 2` shape and the
  identical exposure.

### 4.b — The declared window may be pure fiction
`resolve_context_budget` reads `context_window` off `LLMProfile` (`brain/state.py`) — a
**hardware profile**, not the physically resolved BYOM target. The model actually serving the
call (`core/config/model_resolver.py::get_chat_target(tier).model`, e.g. an Ollama model whose
real `num_ctx` is often 2048/4096 unless the Modelfile overrides it) is never consulted for
its true window. The observed `8192` equals `DEFAULT_CONTEXT_BUDGET` exactly → the profile was
very likely unbound and the system silently used a hardcoded guess. The same magic number is
duplicated in `brain/summarizer.py` (`profile.context_window if profile else 8192`) — at least
three sources of truth for one physical fact. **If the declared window is wrong, every
downstream calculation is wrong, silently.**

### 4.c — No pre-flight check
`prompt_tokens + max_tokens > window` is computable **before** the call, for free. Today it is
discovered as an unparseable response minutes later.

### 4.d — The retry loop actively makes truncation worse
In `agents/planner.py`'s retry loop the corrective is appended **cumulatively**:
`messages[-1]["content"] + corrective`. Attempt 3 carries `prompt + corrective₁ + corrective₂`.
The system's response to *"the output did not fit"* is a **longer prompt**. Negative feedback
loop — the three identical failures were progressively worse attempts.

### 4.e — Already landed (do NOT redo) — uncommitted at time of writing
Green at `ruff` 0 · `mypy` 0/474 · full `pytest` **3119 passed / 2 skipped**:
1. `agents/planner.py::_describe_unusable_draft` — distinguishes "nothing parseable came back"
   from "wrong shape", quoting the raw response head (`_RAW_PREVIEW_CHARS = 400`).
2. `agents/planner.py` retry loop — corrective now **branches**: unusable draft → "assume you
   were cut short, emit a MINIMAL plan"; wrong shape → original field-level corrective.
   Tracked by `_last_draft_unusable`.
3. `tools/llm_gateway.py::ainvoke` — warns on `finish_reason == "length"`, the provider's own
   truncation signal, previously discarded unread.

These make the failure **diagnosable**; they do not create capacity.

### 4.f — The planner divides ONE output budget across N task descriptions
The planner emits every `WBSStep.description` in a **single** completion. The more files it
plans, the shorter each description must be to fit. The coder then receives
`Task: "{description}"` as its primary instruction and faithfully builds exactly that.

**This is the direct mechanism for Discovery B**: more files → thinner descriptions → thinner
code per file. The coder is not blind (it also gets the mission context block with
outcome/decisions/constraints, GraphRAG snippets, and style exemplars — see
`agents/coder.py`'s `_task_preamble` assembly), but the per-step *what to build* instruction is
that one description. A thin instruction yields a thin file regardless of model size.

### 4.g — `validate_output` does not validate the generated code at all ⚠️ **BIGGEST FINDING**
`brain/guardrails.py::run_validate_output_node` — the node the main graph runs after every
apply — does this in full:

```python
output = {
    "vfs_buffer": state.get("vfs_buffer", {}),
    "current_step_id": state.get("current_step_id"),
    "target_role": state.get("target_role"),
}
CoderOutput(**output)          # a Pydantic type check on THREE STATE FIELDS
```

It **never looks at the generated code.** No AST parse, no lint, no execution, no test. A
15-line stub passes trivially — as would an empty file.

The real AST/LSP gates (`syntax_gate` / `style_gate`, `validators/gates.py`) exist but are
wired **only into the MICRO_SWARM topology** (`brain/swarms.py`), a *separate compiled graph*.
The main graph (`brain/engine.py`) goes
`coder_agent → contract_guard → finops_gate → supervisor_node → apply_patch → apply_commit → validate_output`
with no syntax gate anywhere on it.

> **Verify first (this is inference, not yet proven):** confirm which dispatch shape a
> Plan-mode → planner → coder turn actually takes. `brain/intent_router.py::process_user_intent`
> routes `SEQUENTIAL` / `MICRO_SWARM` / `FULL_SWARM`. If Plan-mode turns take the full graph
> — which the topology strongly implies — then **generated code in the primary product path is
> never syntax-checked, never linted, never run.** That is a far larger finding than the token
> budget and would reframe this entire task's priority order.

**Documentation drift, confirm and fix:** `DEVELOPERS.md`'s topology diagram labels this node
`validate_output (AST + LSP)`, and its Coder section claims "Validation happens on a virtual
overlay before anything hits disk: AST parse… LSP lint". Both appear inaccurate for the main
path. Whatever the truth turns out to be, the docs must end this task matching it — a future
session reading that diagram would reasonably assume code validation exists.

### 4.h — The plan's own acceptance `checks` are never executed
`MissionSpecification.checks` is generated by the planner as QA acceptance criteria ("Pytest
exits 0", "the module imports"). A broad grep across `agents/`, `brain/`, `core/`, `tools/`,
`api/` finds exactly **one** consumer:

```
agents/planner.py:877:        len(mission_plan.checks),      # a logger counting them
```

They are never run, never verified, never even shown to the coder while it writes. **The
system declares its own definition of done and then ignores it.** This is arguably the single
cheapest available fix for Discovery B: the acceptance criteria already exist, in structured
form, per plan.

### 4.i — Self-correction is scoped to repair, never to completeness
`WBSStep.requires_iteration` (default **`False`**) routes a step to the agentic cell
(`brain/agentic_cell.py`) — a genuine ReAct loop over a live terminal (run → read → edit →
rerun). The capability is real and shipped. But the planner's own instruction reserves it:

> *"Set 'requires_iteration': true ONLY when it needs an autonomous run-read-edit-rerun loop to
> converge — e.g. fix failing tests, debug a stack trace… Leave it false for trivial
> single-shot edits."*

So "write `HeroSection.jsx`" correctly gets `false` → one-shot generation, no verification
loop. **The self-correction machinery only switches on to fix something already known broken,
never to check whether something new is complete.** Nothing ever asks "is this done?"

---

## 5. Role to adopt

**Systems/inference engineer owning the output contract end to end — capacity *and*
verification.**

You own two invariants:
1. **Capacity:** every request the system issues is one the target model can physically satisfy.
2. **Integrity:** work the system reports as complete has been verified to actually be complete.

That spans profile/target resolution, the context pipeline's input budget, every `max_tokens`
derivation, the retry policy, the shape of the structured output, the validation topology, and
the definition-of-done contract (`checks`). You should be comfortable concluding "the WBS is
too expensive to emit in one shot on this tier" **and** "the main graph has no code validation
at all" — and redesigning the interaction rather than tuning numbers.

Adjacent lenses required:
- **Local-inference reality:** `num_ctx`, KV-cache cost, prompt-eval vs generation rate,
  grammar-constrained decoding (GBNF), and why a 7B at 2–3 tok/s changes which designs are viable.
- **Agent architecture:** when an artifact should be lazily materialized rather than atomic.
  In-repo precedent: 13.0.9 turned whole-turn apply into per-step apply for this exact reason.
- **Verification design:** the difference between *syntactic* validity, *semantic* validity,
  and *sufficiency* — and which of the three each proposed gate actually buys.

---

## 6. Required first pass — MEASURE BEFORE DESIGNING

**Binding.** The previous session's honest conclusion was that the proximate physical cause of
Discovery A is *inferred*, not proven, and Discovery B's mechanism (§4.f) is a strong
hypothesis, not a measurement. Do not skip to implementation.

**Capacity measurements**
- **M1.** Real `num_ctx` of the active local model(s), read from the runtime (Ollama
  `/api/show`, the Modelfile, the served config) — never from a profile we declare.
- **M2.** What `resolve_context_budget()` actually returns in a real Plan-mode turn, and
  whether it came from a bound `LLMProfile` or the `DEFAULT_CONTEXT_BUDGET` fallback. (Strong
  prior: the fallback. Confirm.)
- **M3.** Real `prompt_tokens` for a planner draft after 2–4 grill rounds, and per attempt the
  real `completion_tokens` + `finish_reason`. §4.e.3 now gives `finish_reason` for free.
- **M4.** Token cost of a *complete, valid* `MissionSpecification` for a representative task
  (the portfolio landing page from the live run). Decides whether the monolithic shape is
  viable on this tier at all.
- **M5.** Whether the same budget exposure reaches the coder (`_resolve_coder_max_tokens`) and
  the agentic cell in practice, or only the planner.

**Completion-integrity measurements**
- **M6 (decisive for §4.g).** Trace one real Plan-mode coding turn through the graph and record
  which nodes actually execute. Confirm or refute: the main path runs no syntax gate, no lint,
  no execution against generated code.
- **M7 (separates model capability from architecture — cheap, do it first).** Give the *same*
  local model a rich, explicit single-file instruction ("create `HeroSection.jsx` with: sticky
  nav, headline, subhead, two CTAs, background image with overlay, responsive Tailwind, dark
  emerald/black theme") and compare against what the same model produced from the planner's
  actual terse description. **If the rich instruction yields a complete component, the model is
  fine and §4.f is confirmed as the bottleneck.** If it still stubs out, capability is a real
  co-factor and the design must account for it.
- **M8.** Measure real `WBSStep.description` lengths from live plans, correlated with task
  count `N`. §4.f predicts an inverse relationship. Confirm it.
- **M9.** For a representative failed-in-spirit turn (the landing page), what would the plan's
  own `checks` have caught had they been executed? Establishes the value of §8.7 before
  building it.

**Decision gate:** if M4 > (M1 − M3 − margin), the monolithic plan shape is structurally
non-viable and §8.1 is mandatory. If M6 confirms no code validation on the main path, §8.6
outranks everything in §8 on priority regardless of the budget findings. State which
directions the numbers justify — and which they rule out — before writing code.

---

## 7. Open questions

- **OQ-1.** Should the input budget *reserve* output headroom (`input_budget = window −
  reserved_output − margin`)? Static, per-agent, or derived from expected output shape?
- **OQ-2.** Where does the single source of truth for "this target's real window" live? Today
  it is split across `LLMProfile.context_window`, `DEFAULT_CONTEXT_BUDGET`, and
  `brain/summarizer.py`'s own `8192`. The resolved BYOM target seems the only honest home, but
  it carries no window field today (`core/config/byom_config.py`) — an additive contract change
  under charter §10.
- **OQ-3.** Is plan generation a legitimate forced-escalation trigger in the CSS × TCI matrix
  (`brain/routing_engine.py`)? It is the highest-leverage, lowest-frequency call in the system.
  "Expected output exceeds local tier capacity" is arguably as valid a signal as complexity.
- **OQ-4.** Why do the syntax/style gates live only in MICRO_SWARM? Is that a deliberate
  topology decision with a rationale worth preserving, or drift from an earlier design? The
  answer changes whether §8.6 is "wire the existing gates into the main graph" (cheap) or
  "design a validation stage" (expensive).
- **OQ-5.** Who should own executing `checks` — a new terminal graph node, the existing
  `agents/orchestrator.py` completion path, or the agentic cell? And what happens on failure:
  re-dispatch the offending step, or report honestly and stop?
- **OQ-6.** *Discovered in the same log, unrelated:* zero `server_activity_event` fired during
  the entire 12-minute planner window, though the planner emits `critic_review` /
  `unwrapping_schema` per attempt. Either `narrate` was absent from `config.configurable` on
  the resume path, or events were dropped. Short separate look — **do not let it absorb this task.**

---

## 8. Candidate directions (ranked by leverage, NOT chosen)

### 8.1 — Incremental / lazy plan materialization *(highest leverage; fixes A and B together)*
Bounded skeleton pass (outcome, scope, N task *titles*), then per-step detail expansion right
before each step is dispatched. Converts one unbounded O(N) output into N bounded outputs —
and, critically for §4.f, lets each description be **rich**, because it is no longer 1/N of a
shared budget.

*For:* removes the structural limit; directly attacks Discovery B's mechanism; mirrors the
13.0.9 per-step apply gate; matches how mature coding agents behave.
*Against:* touches `MissionSpecification`'s contract, the `immutable_wbs` drift baseline,
`ValidateWBSDependenciesTool` (needs the whole graph for acyclicity), the plan-document WS
payload, and the frontend checklist. Real blast radius — needs a design pass, not an afternoon.

### 8.2 — Joint, runtime-verified budget arithmetic
`max_tokens = min(declared_ceiling, real_window − prompt_tokens − margin)`; input budget
reserves output headroom; declared window validated against the runtime window, loudly.
*For:* small, contained, obviously correct, benefits every caller.
*Against:* creates no capacity — converts silent truncation into an honest refusal. Necessary,
probably insufficient alone.

### 8.6 — Wire real code validation into the main graph *(priority set by M6)*
If M6 confirms §4.g, this may outrank everything above it: generated code in the primary path
is currently never syntax-checked, linted, or run. `validators/gates.py`'s `syntax_gate` /
`style_gate` already exist and are already wired into MICRO_SWARM — the cheap version is
wiring proven components into `brain/engine.py`, not building new ones.
*For:* highest correctness-per-effort in the whole list if the gates are reusable as-is.
*Against:* adds a node to the hot path (latency/cost per step); needs a retry/route contract
on failure; must not double-run for turns that already go through MICRO_SWARM.

### 8.7 — Execute the plan's own `checks` as the definition of done
The acceptance criteria already exist, structured, per plan (§4.h) — they are simply thrown
away. Running them at turn end (or per step) converts a declared intent into an enforced one,
and gives the coder something concrete to satisfy if surfaced in its prompt.
*For:* cheapest real answer to "hace el trabajo a medias"; needs no schema change; makes the
system's own stated contract honest.
*Against:* checks are free-text and may not be mechanically executable as written — likely
needs a typed/executable subset, plus a policy for unexecutable ones (report vs ignore).

### 8.8 — Let completeness trigger iteration
Today `requires_iteration` only switches on to repair known breakage (§4.i). A sufficiency
signal — a failed check, a suspiciously small artifact vs. its description, an unmet acceptance
criterion — could route a step into the existing agentic cell instead of accepting a stub.
*For:* reuses shipped machinery; closes the "nothing ever asks if it's done" gap.
*Against:* needs a trustworthy sufficiency signal or it thrashes; interacts with the budget
work (iteration costs more tokens on the tier least able to afford them).

### 8.3 — Grammar-constrained decoding (GBNF)
*For:* changes the failure mode from unparseable garbage to **valid-but-incomplete** JSON, of
which a truncated task prefix is salvageable.
*Against:* backend-specific; does not prevent truncation; adds capability detection.

### 8.4 — Route the planner up a tier
*For:* cheapest real capacity fix; the routing matrix exists.
*Against:* contradicts local-first if unconditional; needs a principled trigger (OQ-3).

### 8.5 — Slim the output schema
Terser wire shape (short keys, no prose the coder re-derives), expanded locally after parse.
13.0.9 already moved `agent_notes` off the human-facing `description` — same direction.

### 8.9 — Streaming + incremental structured parse
Know at token N that the budget will run out and degrade deliberately; enables partial
recovery. Same architectural move DEBT-194 flagged for liveness detection — check whether the
two should land together.

---

## 9. Files in scope

**Primary — capacity**
- `ailienant-core/brain/agent_context.py` — `resolve_context_budget`, `DEFAULT_CONTEXT_BUDGET`
- `ailienant-core/agents/planner.py` — `_resolve_planner_draft_max_tokens`, the retry loop, and
  the **hand-written** JSON contract in the instruction (note: the prompt is NOT derived from
  the Pydantic schema — changing `WBSStep` does *not* change what the model is asked for)
- `ailienant-core/agents/coder.py` — `_resolve_coder_max_tokens`, `_task_preamble` assembly
- `ailienant-core/tools/llm_gateway.py` — `ainvoke` budget/timeout resolution,
  `_extract_nested_schema_target`, `resolve_local_timeout`, the new `finish_reason` warning
- `ailienant-core/core/config/model_resolver.py`, `core/config/byom_config.py` — target
  resolution; candidate home for a real per-target window (OQ-2)
- `ailienant-core/brain/state.py` — `LLMProfile.context_window`, `MissionSpecification`,
  `WBSStep` (`description`, `requires_iteration`, `checks`)

**Primary — completion integrity**
- `ailienant-core/brain/guardrails.py` — `run_validate_output_node` (§4.g), `route_after_validation`
- `ailienant-core/brain/engine.py` — main graph topology; where a real validation stage would wire
- `ailienant-core/brain/swarms.py` + `ailienant-core/validators/gates.py` — the existing
  `syntax_gate` / `style_gate`, currently MICRO_SWARM-only
- `ailienant-core/brain/intent_router.py` — `process_user_intent` dispatch (needed for M6)
- `ailienant-core/brain/agentic_cell.py` — the shipped ReAct loop (§8.8's target)
- `ailienant-core/agents/orchestrator.py` — completion path; candidate owner for `checks` (OQ-5)
- `ailienant-core/tools/validation/` — `diagnostics.py`, `lsp_filter.py` (the real linters)

**Secondary / blast radius before touching the schema**
- `ailienant-core/brain/context_pipeline.py` — `ContextBudgetError`, the 5-layer assembler
- `ailienant-core/brain/summarizer.py` — duplicate `8192` fallback
- `ailienant-core/brain/routing_engine.py` — CSS × TCI matrix (OQ-3)
- `ailienant-core/tools/planner_tools.py` — `ValidateWBSDependenciesTool` (needs the full graph)
- `ailienant-core/brain/drift_monitor.py` + `immutable_wbs` — a lazily-materialized plan changes
  what "the plan" means to the drift baseline
- `ailienant-core/api/ws_contracts.py` — `PlanDocumentPayload` if the plan becomes progressive
- `ailienant-extension/src/workspace/components/ExecutionChecklist.tsx` — renders the WBS
- **`DEVELOPERS.md`** — topology diagram + Coder section both misdescribe validation (§4.g);
  must end this task accurate

**Reference (read, do not edit here)**
- `docs/SCHEMA_EVOLUTION.MD` §56 — 13.0.9 channels, precedent for contract change
- `docs/PHASE_8_16_BLUEPRINT.md` — compaction gate criterion and its correction
- `docs/TECH_DEBT_BACKLOG.md` — DEBT-191…196 (the local-hardware/timeout family)

---

## 10. What to avoid

1. **Do not "fix" this by raising `max_tokens` or the timeout.** The timeout was never hit
   (242 s of a 2108 s budget). Raising ceilings against a window that cannot hold them makes
   the failure slower, not rarer.
2. **Do not treat Discovery A as a schema/contract bug.** `input_value={}` is an unwrapper
   artifact, not a Pydantic or `MissionSpecification` defect. §4.e.1 removed that misdirection
   — do not reintroduce it.
3. **Do not go down the compaction path.** §3 — ruled out with measurement (0 events ever).
4. **Do not redo §4.e.** Those three mitigations are landed and green.
5. **Do not assume the 13.0.9 batch caused this.** Audited: `tools/llm_gateway.py` untouched,
   the planner prompt is hand-written (so the new `WBSStep.agent_notes` never reaches the
   model), and the only planner change runs *after* validation succeeds. A dead end.
6. **Do not trust `DEVELOPERS.md`'s claim that `validate_output` does AST + LSP.** Read
   `run_validate_output_node` yourself (§4.g). Assuming validation exists where it does not is
   the exact error that let Discovery B ship.
7. **Do not skip §6, and do M7 first** — it is the cheapest measurement in the list and it
   determines whether this is an architecture problem or a model-capability problem. Building
   on the wrong answer wastes the whole session.
8. **Do not silently widen a wire contract.** Charter §10: additive, version-tagged only.
   `MissionSpecification` is persisted and checkpointed — older checkpoints must rehydrate.
9. **Do not let OQ-6 (missing narration) absorb the session.** Note it, scope it separately.
10. **Do not fix only Discovery A.** It is the loud one and the tempting one. Discovery B is
    what the operator actually experiences as "the product does half the job."

---

## 11. Charter constraints binding this task

- **§1** — read `docs/PROJECT_MANIFEST.md`, `DEVELOPERS.md`, `docs/DEV_JOURNAL.md` first;
  resolve the active phase from the manifest rather than assuming one.
- **§3** — `Core / Eval / Brain` is the deterministic engine: pure determinism, immutability,
  no hidden global mutation across an `await`, and **token hygiene before any I/O enters an
  agent's context** (§5.5). This task *is* token hygiene; it should exemplify the rule.
- **§8** — Zero-Degradation: `npx pyright` + `mypy .` must not gain a single new error. Test
  taxonomy applies — hermetic stubs at the engine boundary, real integration tests for
  contract-critical paths.
- **§10** — wire contracts additive-only and version-tagged; `MissionSpecification` is persisted.
- **§11** — any MVP compromise declared explicitly and logged as debt in the same change.
- **§12** — observability: a log line must let a future reader reconstruct what happened. §4.e
  exists because that rule was violated; do not violate it again.
- **§13** — no phase/ADR references in code comments; English only.
- **§14** — phase-closure ritual (manifest checkbox, one strict journal entry, schema section,
  `DEVELOPERS.md` when structure changes). Git is **non-autonomous**: provide the command
  block, never run it.

---

## 12. Definition of Done

1. §6's measurements are recorded in the plan, and the design explicitly names which §8
   directions they justify **and which they rule out** — with M6 and M7 called out by name,
   since they set the priority order.
2. Gates green: `ruff check .` · `mypy .` · `npx pyright` · `pytest` · `npm run compile` ·
   `npm run lint` · `npm test`.
3. New regression tests failing on the *old* behavior, minimally covering:
   - the joint-budget invariant (input + output ≤ real window) and the pre-flight refusal path;
   - if §8.1 lands: a plan materializing across several passes that the drift monitor and
     dependency validator still accept;
   - if §8.6 lands: syntactically invalid generated code is **caught on the main graph path**
     (a test that would have passed silently before);
   - if §8.7 lands: a plan whose `checks` fail does not report success.
4. **Live re-test of both discoveries on the same local hardware:**
   - **A:** Plan mode, 3+ grill rounds → a complete valid plan, or an honest actionable refusal
     — never a silently truncated one.
   - **B:** "build a complete landing page" → either files with substantive implementations, or
     an explicit, honest report of what was not completed and why. **A stub that reports
     success is a failed DoD.**
5. The silent-degradation case from §2 has an explicit answer: either it is detectable, or it
   is logged as declared debt with reasoning.
6. `DEVELOPERS.md`'s validation claims match reality (§4.g / §9).
7. Docs per §11/§14, including a `SCHEMA_EVOLUTION.MD` section if any contract moves, and debt
   entries for anything consciously deferred.

---

## 13. One-paragraph summary (for a cold start)

Two failures, one root-cause family. **Loud:** a Plan-mode turn died three times with a
misleading "6 validation errors … `input_value={}`" message; forensics proved the timeout never
fired (242 s of a 2108 s budget) and the input side was never under pressure (1180 of 8192
tokens) — the model returned an unparseable, almost certainly truncated draft, and the
unwrapper's empty dict masqueraded as a schema error. **Silent, and worse:** asked for a
complete landing page, the system produced several files each containing only a thin stub, and
every gate passed. Compaction was tested as a hypothesis and **ruled out with data** — it has
never fired once in real usage (0 events, utilization 0.09–0.14). The real causes are that
output tokens can be neither retrieved nor compressed (only reduced or split) while every
defense this system has manages input; that the planner divides one output budget across N task
descriptions, so more files means thinner instructions and thinner code; that
`run_validate_output_node` performs a Pydantic type check on three *state fields* and never
looks at the generated code at all, with the real syntax/lint gates wired only into a separate
MICRO_SWARM graph; that the plan's own acceptance `checks` are generated and then referenced
exactly once, in a logger counting them; and that self-correction only ever switches on to
repair known breakage, never to ask whether new work is complete. Diagnostic mitigations for
the loud failure already landed. The open task is to decide — **from measurements, not
inference** — how much of this is capacity (fix the budget arithmetic, or make plan generation
incremental) versus verification (wire real validation into the main path, and enforce the
definition of done the system already writes for itself).
