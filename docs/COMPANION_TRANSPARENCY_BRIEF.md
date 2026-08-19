# Companion Transparency — Design Brief

**Status:** Brief only. No implementation in this document or its companion commit —
this captures the goal and frames the open design questions to work through together
before any code changes. Tracked as DEBT-183.

## The goal, in the user's own words

> Quiero que haya visibilidad y transparencia en todo el flujo de trabajo que se
> visualiza en el chat. Cada vez que se envíe un prompt se vea todo lo que está
> sucediendo por detrás — que el usuario vea cómo el sistema es inteligente, cómo
> razona consigo mismo, qué acciones toma, por qué y cómo toma decisiones, qué está
> haciendo el coder companion, etc. Incluso en modo Plan (investigación) se debe ver
> cómo está investigando; cuando escribe código, que indique si consigue errores, cómo
> los corrige, cómo toma decisiones — todo eso, paso a paso, no todo de una vez al
> final.

Concretely, today in Plan mode a prompt lands and the chat shows one generic
"Understanding your request" line, then nothing, until the question card appears —
even across several internal grounding/generation rounds (partially closed by the
narration added in 13.0.6, see `docs/DEV_JOURNAL.md`, but that only covers the grill's
own two phases, not the broader ambition here).

## What already exists — read this before designing anything new

Two mechanisms already do *part* of this job, and any redesign has to sit correctly
between them rather than duplicate either:

**1. The Glass-Box Timeline** (`server_activity_event` → `AgentTimeline.tsx` /
`activityLabels.ts`) is already mode-agnostic and already un-throttled (explicitly
exempt from the `NarrationGate`, `api/ws_contracts.py`). It already fires from
`ideation.py`, `planner.py`, `coder.py`, and `error_correction.py`. It carries a closed
vocabulary of *what* happened — `understanding, planning, reviewing, read, edit,
command, retrieval, heal, reasoning, plan, diff, cell` — with a target and a metric,
per step, in order, as it happens. This is the "paso a paso, no todo de una vez"
requirement, largely solved already for the *what*.

**2. The Coder Companion** (`brain/coder_companion.py`) is a single, post-hoc LLM call
(`_run_coder_companion`) that fires once, after `agents/coder.py` finishes producing
`pending_patches` — the *only* call site in the codebase. It answers *why*: objective,
decisions, patterns applied, bottlenecks, security notes, errors found, follow-ups,
reasoning summary. That's real value the timeline doesn't carry. But:

- Its input contract is patch-shaped (`_build_companion_request` reads
  `pending_patches`/`pending_contents`) and its system prompt is patch-specific — it
  cannot currently explain a Plan-mode interview or a research turn, because it has
  nothing patch-like to look at.
- It emits one complete blob at the end (`CoderCompanionPayload`,
  `api/ws_contracts.py:1548-1576`), not incrementally — `workspaceStore.ts`'s
  `coderCompanions` map *replaces* by `task_id`, with no append/streaming semantics.
- The frontend card is hard-gated on `diffBlocks.length > 0`
  (`Workspace.tsx:698-700`), so it structurally cannot mount outside a turn that
  produced a diff, independent of anything on the backend.

So: **the "what, step by step" half of the ask is largely built and mode-agnostic
already; the "why" half exists only for code-writing turns, arrives once at the end,
and is invisible everywhere else.** The redesign is about the second half, and about
deciding whether/how it should ride alongside the first rather than duplicate it.

## Open questions to resolve together

1. **Input contract.** What does the companion look at when there's no patch to
   explain? A Plan-mode round has a question batch and grounding trace instead of a
   diff; a pure research/Ask-mode turn has retrieval results and reasoning instead of
   either. Does one generalized contract cover all three, or does each mode get its
   own shaped request feeding a shared explanation model?

2. **Granularity: per-step or per-turn?** The literal ask is "paso por paso, no todo
   de una vez al final." Does that mean the companion itself becomes incremental
   (one explanation per timeline step — expensive, since it multiplies LLM calls by
   step count), or does it mean the *timeline* stays the step-by-step surface (already
   true) and the companion's job is a good, promptly-delivered *summary* attached to
   the round/turn that just happened, not literally streamed token-by-token? These
   read very differently in cost and complexity.

3. **Division of labour with the Timeline.** If the companion starts explaining
   *what* happened too, it will drift into duplicating `AgentTimeline`. Proposed
   framing to test: Timeline = what + when (cheap, structural, already free of LLM
   cost per step); Companion = why + so what (expensive, LLM-authored, worth gating).
   Does that boundary hold across Plan/Ask/Auto, or does one mode need something in
   between?

4. **Cost and latency governance.** Today's guards — budget check, VRAM-slot
   contention, a 3-way concurrency semaphore, 12s cloud / 45s local timeout — exist
   because even ONE post-hoc call per coding turn is worth rate-limiting. Whatever
   granularity comes out of question 2, the same governance has to scale with it
   without becoming a second FinOps surface to maintain.

5. **Frontend delivery.** Removing the `diffBlocks.length > 0` gate is mechanical.
   Deciding what the card (or a new surface) looks like when there may be several of
   these per turn, interleaved with timeline rows and possibly with clarification
   cards, is not — it needs a rendering position, not just a removed condition.

## Non-goals for this brief

- No contract changes, no new WS events, no frontend gate removal yet — this is
  scoping, not the change itself.
- Not a replacement for the Timeline; explicitly keep both channels distinct.
