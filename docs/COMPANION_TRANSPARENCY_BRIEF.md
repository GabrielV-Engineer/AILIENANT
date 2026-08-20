# Companion Transparency — Design Brief

**Status:** RESOLVED 2026-08-20 (13.0.7). DEBT-183 closed. This brief's five open
questions were answered and implemented — see `docs/DEV_JOURNAL.md`'s 13.0.7 entry and
`docs/PROJECT_MANIFEST.md` §13.0.7 for the shipped design. Kept below as the historical
record of the goal and the reasoning that shaped the answers; do not treat anything
here as still-open.

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

## Open questions — resolved 2026-08-20 (13.0.7)

1. **Input contract. RESOLVED:** each mode got its own shaped request, not one
   generalized contract — three new builders (`build_ideation_companion_request`,
   `build_planning_companion_request`, `build_healing_companion_request`) each read
   only their own decision point's data, feeding the same `CompanionAnalysisRequest`
   carrier via a new `scope`/`scope_summary` pair.

2. **Granularity. RESOLVED, and NOT what this brief predicted:** neither per-step nor
   per-turn — per real graph decision point (a grill round closing, a plan committing,
   a patch landing, error correction resolving), each already topology-bounded. The
   deciding factor turned out to be *latency*, not cost: per-step calls, serialized
   behind the existing semaphore, would desynchronize badly from the actual step. A
   separate, more valuable fix rode alongside: `AgentTimeline` was only ever rendering
   the FIRST reasoning entry of a turn (a latent bug, not a scoping choice) — fixing
   that gives the literal "paso a paso" reasoning trace at zero LLM cost, via the
   primary model's own thinking, not the Companion.

3. **Division of labour. RESOLVED, framing held exactly as proposed:** Timeline =
   what/when (free), the primary model's own reasoning = why inline (now free, see
   above), Companion = and-so-what (expensive, gated, decision-point-scoped).

4. **Cost/latency governance. RESOLVED:** the existing budget/VRAM/semaphore guards
   are reused unchanged for every scope; a new shared `_MAX_COMPANION_EMISSIONS_PER_TASK`
   backstop was added since decision points, unlike a single post-hoc call, can recur
   through a cyclic subgraph (`coder ↔ error_correction`, the grill self-loop).

5. **Frontend delivery. RESOLVED:** the `diffBlocks.length > 0` gate was dropped;
   companion storage moved from a session-wide store to message-scoped append storage
   (`Message.companions`), rendering in the same position as before (beside the diff /
   inside the per-message stack) as a stack of per-decision-point cards.

## Non-goals for this brief

- No contract changes, no new WS events, no frontend gate removal yet — this is
  scoping, not the change itself.
- Not a replacement for the Timeline; explicitly keep both channels distinct.
