# Phase 11.11 Blueprint — Agent Output Quality & Narration Depth

**Status:** ACTIVE — binding for all work under manifest item 11.11.
**Scope:** the five *behavioral* defects surfaced by two live end-to-end runs on local models in AUTO mode. The seven *mechanical* defects from the same runs shipped separately as 11.10 and are out of scope here.

---

## 1. Rationale

Two live runs — "build an MVP game from this GDD", then "explain the code you wrote" — exposed a class of defect distinct from the correctness bugs 11.10 fixed. Nothing crashed and no contract broke; the system did exactly what its prompts and budgets told it to, and the result was still poor:

- the Thought Box sat empty for the entire coding turn, so the user watched a silent progress bar;
- narration was deterministic status strings only — structurally honest but robotic;
- identifiers from an unrelated project in the same workspace root bled into the generated game code;
- an unconstrained "build a game" request produced a Django/React scaffold;
- generated files were stubs;
- an explicit "explain it to me in detail" request returned a terse summary.

These are prompt-, budget-, and context-assembly defects. They are fixed by changing what the agents are *told* and *allowed to produce*, not by repairing broken logic.

## 2. Corrected root causes (binding — the original brief was wrong on three)

An initial audit misattributed three of the five. The corrections below are authoritative; the named non-causes must not be "fixed".

| Symptom | Original (wrong) attribution | Verified cause |
|---|---|---|
| Over-simplified answers | `brain/fast_path.py` caps answers at `max_tokens=512` on `MODEL_SMALL` | **`fast_path.py` is not in the question path at all.** `execute_sequential_bypass` is reachable only from `brain/intent_router.py` under SEQUENTIAL *execution mode*. The `question` intent routes to `task_service._stream_chat_answer`, which passes **no `max_tokens`**. The brevity is authored: `_CHAT_SYSTEM_PROMPT` instructs *"directly and concisely"* and *"explain the key decisions briefly"*. |
| Shallow plans | `_PLANNER_REASONING_MAX_TOKENS = 512` starves planner reasoning | **That constant budgets the pre-draft narrative shown while the user waits, not the plan** — its own comment says so. The WBS draft is a separate `acomplete_with_thinking` call that passes no `max_tokens` and therefore takes the gateway default 4096. |
| Cross-project bleed | Planner injects an unbounded folder tree; coder RAG is unscoped | **Both are already bounded.** `build_workspace_overview` caps at depth 3 / 100 files / 2048 chars, and the coder's RAG query is task-scoped (`f"{target_file} {description}"`, k=3). The real vector is **`project_id` granularity**: one opened workspace root spanning two unrelated projects indexes both under a single hash, so both are legitimately in scope. |

**Do not modify** as part of this phase: `brain/fast_path.py`, `_PLANNER_REASONING_MAX_TOKENS`, or any of the tight judge/telemetry/compression budgets enumerated in §4.

## 3. Item A — Analyst narration into the Thought Box

**Locked decision (do not relitigate):** the Coder gets **no** `<thinking>` scaffold. Its output is a strict SEARCH/REPLACE marker contract, and `tools/llm_gateway.py` deliberately keeps reasoning scaffolds away from strict machine-parsed contracts because mixing them corrupts patches. This is a standing invariant, previously recorded as the DEBT-013 recurrence class.

**Design constraint discovered in exploration:** the existing companion call is *also* a strict contract — `_call_analyst_llm` requests JSON and `_parse_companion_json` validates it — so its token stream cannot be piped to the Thought Box as prose. Streaming it verbatim would render raw JSON on screen.

**Resolution — a second, free-form pass.** `brain/coder_companion.py` gains a narration call that is separate from the structured analysis call:

- it uses `LLMGateway.astream_reasoning(..., free_form_answer=True)` — the same mechanism `agents/planner.py` already uses for its pre-draft pass — and streams deltas to the existing Thought Box sink;
- the structured `CompanionAnalysis`, the `server_coder_companion` WS contract, and `CoderCompanionCard` are **unchanged** (§10 additive-only holds trivially: no contract is touched);
- it inherits every 11.5.B safety property: fire-and-forget, no blocking graph edge, semaphore-bounded, GC-safe strong-reference set, and the 11.10 tier-aware timeout ladder (45 s local / 12 s cloud) with a guaranteed terminal broadcast on every exit path.

**Accepted tradeoff (charter §11):** this is a *second* LLM call per coding turn. On a local tier that is real latency and VRAM contention. It is therefore gated behind the existing `_companion_budget_available` and `_companion_gpu_slot_available` checks and must **skip**, never queue, under pressure. Narration is a comfort feature; it may never delay or degrade the code path.

The deterministic status strings (`_format_coding_summary`, apply banners, `activityLabels.ts`) remain as fast structural anchors. The narration rides on top of them rather than replacing them, so a skipped narration degrades to today's behavior exactly.

## 4. Item C — budget policy

**Principle:** scale *result-bearing* budgets by task complexity; keep judge, telemetry, and compression budgets tight (charter §5.5). A flat global bump is explicitly rejected — it would raise cost and local latency on every trivial turn to fix a problem that only appears on broad ones.

Two genuine gaps, both of the form "no `max_tokens` passed → gateway default 4096":

1. **Coder generation** — 4096 is shared across every file *plus* scaffolding in a single WBS step, which is what produces stub files.
2. **Planner WBS draft** (`acomplete_with_thinking`) — same default; a broad build-out needs more room than a rename.

Both derive their budget from task complexity and are bounded by the resolved model's real context window, never a hardcoded ceiling.

**Answer depth** is a prompt fix, not a budget fix: `_CHAT_SYSTEM_PROMPT` keeps its concise default, and swaps in an expansive variant when the request carries explanation signals (reusing `_EXPLAIN_SIGNALS`, introduced in 11.10 for intent classification). A one-line question still gets a one-line answer.

**Verified-correct budgets that stay untouched:** analyst nightmare/supreme 120, intent-reflect 60, rule-distill 80, summarizer 512 (compression by design), researcher→planner 2048, intent classifier 20, BYOM health 5, companion 220/420/800 (already verbosity-scaled), `mcts_coder` (dormant, DEBT-009), `_PLANNER_REASONING_MAX_TOKENS` 512.

## 5. Items B, D, E — context and planning guidance

**B — topic relevance.** Add a relevance filter before injection rather than raising any cap. Candidate files and snippets are scored against the active task and off-topic entries dropped; the workspace overview is biased toward the subtree the task concerns. **Hard constraint:** an explicit user reference always wins — a file named by path (`explicit_mentions`, `active_file_path`) is never filtered out, regardless of score. A relevance filter that can hide a file the user explicitly asked about is a worse defect than the bleed it prevents.

**D — stack choice.** No stack guidance exists anywhere in the prompt surface today, so the model's own prior decides unchallenged. The planner gains guidance to infer the stack from the **artifact class** (a game implies a game engine, not web CRUD) for unconstrained requests, or to confirm the stack briefly before committing a WBS.

**E — proportional scope.** `_SCOPE_DISCIPLINE_DIRECTIVE` currently asserts that the smallest WBS is always correct, which under-plans broad requests. It becomes proportional — minimal for narrow or named-file requests, adequately deep for broad build-outs. **The "injected context is READ-ONLY reference; seeing a file is never a reason to edit it" clause is retained verbatim** — that half is what prevents sprawl and is load-bearing for Item B.

## 6. Interaction between items

C and E compound deliberately: a proportionally deeper plan (E) multiplied by a complexity-scaled per-step budget (C) is what turns a stub scaffold into a real MVP. Either alone is insufficient — a deep plan with a 4096 budget still emits stubs, and a large budget against a two-step plan still under-builds.

B and E are in tension by construction and must be verified together: E loosens how much the planner may build, while B narrows what it is allowed to see. The retained READ-ONLY clause is the seam that keeps them compatible.

## 7. Verification

Gates (charter §2, all exit 0): `npx pyright` · `mypy .` · `pytest` · `npm run compile` · `npm run lint` · `npm test`.

The one regression that invalidates Item A: coder SEARCH/REPLACE patches must still apply cleanly with narration active. Patch corruption means the reasoning/contract separation leaked, and the item is reverted rather than patched.

Behavioral acceptance is a re-run of the original GDD-game test — see the manifest item's DoD for the full checklist.
