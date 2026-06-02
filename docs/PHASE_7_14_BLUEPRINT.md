# PHASE 7.14 — Master Architectural Blueprint (UI/UX Transformation to Enterprise Agent: Zero-Bubble & Full-Cognition)

> **Mandatory read** during every Phase 7.14 task. Survives session compactions: re-derive intent from this document. Any deviation from the binding decisions tagged **[ADR-72x]** requires an explicit blueprint amendment in the same PR.
>
> **Frontend track.** 7.14 lives almost entirely in `ailienant-extension/` and is **orthogonal to the active backend 8.0.0** (`mypy --strict` surface debt). The two tracks do not collide; 7.14 ships no Python contract change (ADR-721).

## Context

Phases 7.10–7.13 + 9 made AILIENANT a *connected, self-healing, event-driven* product. What remains is the **last mile of perception**: the panel still reads as a **chatbot** (centered bubbles, no real diffs, plain `<pre>` code) rather than an **integrated code agent** at Cursor / Claude-Code fidelity. Phase 7.14 closes the visual + interaction gap.

A three-front audit (rendering pipeline · diff/HITL/backend contracts · the full agentic-UX surface) established that **the project is already far more mature than a "transform from scratch" brief assumes** — ~20 of the 25 surveyed elite-IDE techniques are already implemented at production quality. Therefore 7.14 is scoped, per CLAUDE.md §3 (Strategic Auditor) and the project's Zero-Deduplication rule, as **two net-new epics + three enhancements + one strategic-gaps slice** — never a rebuild of mature systems (that is pure regression risk).

### The audit (current state vs. brief premise — verified in code)

| Brief epic | Already exists (reuse) | The real 7.14 work |
|---|---|---|
| **Zero-Bubble canvas** | Messages are styled as bubbles: `.ws-msg` `border-radius:4px` / `max-width:88%` / role backgrounds + `align-self:flex-end` ([workspace.css](../ailienant-extension/src/workspace/workspace.css)); `MarkdownRenderer` is fully decoupled from that CSS | **NET-NEW** — strip the bubble chrome, go full-width "document", hairline separators, subtle role label, documentation-grade prose typography |
| **Elite Diff Engine** | Chat renders code as **plain `<pre><code>` with no syntax highlighting and no diff** ([MarkdownRenderer.tsx](../ailienant-extension/src/workspace/components/MarkdownRenderer.tsx)); Monaco `DiffEditor` exists **dashboard-only** ([StagingArea.tsx](../ailienant-extension/src/dashboard/panels/StagingArea.tsx)). Structured edit data already flows: `ApplyWorkspaceEditPayload{file_path,new_content,base_hash}` ([ws_contracts.py](../ailienant-core/api/ws_contracts.py)); the host already reads the **old** doc text to compute the stale-hash ([PatchActuator.ts:73-75](../ailienant-extension/src/core/PatchActuator.ts#L73)) | **NET-NEW (crown jewel)** — inline split-diff with hatching + contextual file-path header, fed by the existing edit payload, diffed client-side |
| **Ghost Telemetry** | Mature: status-pill tool chips ([ToolChip.tsx](../ailienant-extension/src/workspace/components/ToolChip.tsx)), live thinking + frozen elapsed ([ThoughtBox.tsx](../ailienant-extension/src/workspace/components/ThoughtBox.tsx)), OCC/TPS/FinOps HUD ([TelemetryHUD.tsx](../ailienant-extension/src/workspace/components/TelemetryHUD.tsx)), PipelineProgress | **ENHANCE** — status dots, a live action-log while thinking, a per-message live-token footer (today only a *final* count) |
| **HITL Approval** | Mature: rich approval card with edit-mode, risk pills, `Ctrl+Enter`/`Esc`/`F2` ([HITLInterventionCard.tsx](../ailienant-extension/src/workspace/components/HITLInterventionCard.tsx)); `HITL_RESPONSE` ↔ `client_hitl_response` round-trips with `comment`/`modified_content` | **ENHANCE** — co-locate Accept/Reject/Comment under each diff; nested re-prompt that preserves the draft; keyboard on a focused diff |
| **Procedural Memory** | @-mentions mature (trie-backed, files+folders, caret-anchored — [MentionDropdown.tsx](../ailienant-extension/src/workspace/components/MentionDropdown.tsx) + [workspacePathIndex.ts](../ailienant-extension/src/providers/workspacePathIndex.ts)); checkpoints/rewind mature ([CheckpointPicker.tsx](../ailienant-extension/src/workspace/components/CheckpointPicker.tsx) + `BRANCH_FROM_CHECKPOINT`) | **SURFACE/ENHANCE** — an inline per-message Revert that reuses branch-from-checkpoint; minor @-mention polish |

### Grounding (existing infrastructure to reuse, verified)

- **Edit pipeline** — `server_apply_workspace_edit` → `PatchActuator.apply()` ([workspace_panel.ts:507](../ailienant-extension/src/providers/workspace_panel.ts#L507)); `PatchActuator._openExisting()` + `doc.getText()` already yields the **old** content at [PatchActuator.ts:73-75](../ailienant-extension/src/core/PatchActuator.ts#L73). This is the diff enrichment seam (ADR-721) — old+new in hand, host-side, no Python touch.
- **Markdown/code** — `MarkdownRenderer` block parser ([MarkdownRenderer.tsx](../ailienant-extension/src/workspace/components/MarkdownRenderer.tsx)) extracts the fence language but does not highlight; it is decoupled from `.ws-msg` so the canvas can change independently (ADR-720). 7.14.2 adds highlighting here and in the diff.
- **HITL** — `HITLResponsePayload{approved,comment,modified_content}` ([ws_contracts.py](../ailienant-core/api/ws_contracts.py)) + the in-chat card + native-toast dedup (`HitlNotifier._resolved`) — the inline per-diff actions reuse this exact channel (ADR-724); no new event.
- **Checkpoints** — `Message.checkpoint_id` + `MessageActions` branch button + `BRANCH_FROM_CHECKPOINT` → `client_branch_from_checkpoint` ([workspace_panel.ts](../ailienant-extension/src/providers/workspace_panel.ts)) — the inline Revert is a *surfacing* of this (ADR-725).
- **Token accounting** — backend tracks `token_usage` + `active_llm_profile.context_window` (Fase 2.26.2 ContractGuard triggers) — the data source for the context-budget meter (ADR-726).
- **Theme** — VS Code injects `var(--vscode-*)` into the webview; `.ws-md-pre`/`.ws-mini-terminal` already key off `--vscode-editor-background` / `--vscode-editor-font-family` — the theming contract the diff red/greens extend (ADR-722).
- **Lifecycle** — the 7.12 inflight-snapshot + per-session draft pattern ([workspaceStore.ts](../ailienant-extension/src/workspace/workspaceStore.ts)) — diffs derive from persisted edit payloads, not transient state, so a WebView teardown mid-stream re-hydrates cleanly.
- **esbuild** — three build contexts (extension CJS / webview IIFE / dashboard ESM+splitting) ([esbuild.js](../ailienant-extension/esbuild.js)); the webview IIFE bundle is the one shiki must not bloat (ADR-722).

This blueprint is the contract that future 7.14 PRs must conform to.

---

## 1. Scope boundary (what 7.14 owns)

| Owns | Does NOT own |
|---|---|
| The Zero-Bubble canvas (layout/typography/separators/role labels); the inline Elite Diff Engine (split-diff + hatching + contextual header) fed by the existing edit payload and diffed client-side; the host-side diff enrichment (old+new → webview message); syntax highlighting in chat code + diffs; Ghost-Telemetry enhancements (status dots, live action-log, live-token footer); inline per-diff Accept/Reject/Comment + nested re-prompt + diff-focus keyboard; the inline checkpoint Revert affordance; the context-budget meter + auto-accept toggle; minor @-mention polish. | Any **Python contract** change (`ws_contracts.py`, `AIlienantGraphState`, payload shapes stay immutable — ADR-721). A second diff *source* (no `server_chat_diff` event). True **per-hunk** approval IDs (deferred — would need backend). New cognition/agents. Parallel multi-thread runs, cross-session context refs, dual-mode CLI (**deferred to Phase 11**, ADR-726). The dashboard Monaco diff (stays dashboard-only). |

---

## 2. Binding Decisions (ADRs)

> Contiguous **720 → 726** (708–718 belong to 7.13; 719 intentionally unused as the boundary marker).

### [ADR-720] Zero-Bubble Infinite Canvas

The chat abandons the bubble paradigm for an **interactive code document**. Binding rules: (a) **no** per-message border, border-radius, role background, or `max-width`/`align-self` — content spans **100%** of the panel and grows when the panel is widened; (b) turns are separated by an **ultra-thin hairline** (`border-bottom: 1px solid rgba(255,255,255,0.05)` — theme-tuned); (c) user vs assistant differ **only** by a subtle role label/icon (decision locked — no bubbles, no role backgrounds); (d) **typography is dual-density** — prose gets documentation-grade `line-height` while code blocks stay compact. `MarkdownRenderer` is untouched by this ADR (CSS-only + a per-turn role-label wrapper). *Rationale:* code needs to breathe; bubbles waste horizontal space and impose a messaging metaphor on a code surface. *Anti-patterns:* re-introducing role tints "for contrast"; centering the transcript under a narrow `max-width`.

### [ADR-721] Diff data via existing edit payload + client-side jsdiff (NO backend contract change)

Inline diffs are fed by the **already-flowing** `ApplyWorkspaceEditPayload` (`file_path` + full `new_content` + `base_hash`). The **host** enriches at the `server_apply_workspace_edit` seam: since `PatchActuator` already opens the target doc and reads its current text, the host posts a new **webview** message `RENDER_DIFF {patch_id, file_path, old_content, new_content, status}` to the panel (in addition to / ahead of applying, depending on mode). The diff is computed **client-side with `jsdiff`** in the webview. **No `ws_contracts.py` / Python change**, no new server event, no second data path. *Rationale:* the structured edit + old content already exist host-side; a new backend event would duplicate a working pipeline. *Anti-patterns:* parsing diffs out of markdown fences as the primary path; minting a `server_chat_diff` event; sending file contents the host doesn't already have.

#### [ADR-721 · Amendment A] Additive read-only telemetry surfacing for the context meter

The context-budget meter (ADR-726) needs the model's `context_window` and the **live** window occupancy,
neither of which crossed to the webview. A strategic audit confirmed the only token data reaching the
panel was the cumulative `TokenSnapshot` (lifetime ledger cost/savings) — insufficient and, if used as a
proxy, actively misleading: the ledger is a monotonic sum `L_total = Σ Tᵢ` whereas LangGraph runs a
pruned/summarized window `O_current = Σ_{i=n-k}^{n} Tᵢ ≤ C_max`, so a ledger-fed meter would pin to 100%
in long sessions even after the graph pruned context. **Decision:** ADR-721's "no Python contract change"
is amended to permit **additive, read-only** surfacing — a new `GET /api/v1/sessions/{thread_id}/context`
route returning `{context_window, context_used_tokens, context_pct}`, computed by tokenizing the current
checkpointed message window. This adds **no WS event**, no `ws_contracts.py` change, and no graph-state
shape change; the diff pipeline's ADR-721 invariants are otherwise untouched. The frontend gate (a
ledger proxy) was **explicitly rejected** — telemetry accuracy is non-negotiable.

### [ADR-722] Diff render stack + theming + bundle discipline

Stack: **`jsdiff`** (line/word math) · **`react-diff-viewer-continued`** (asymmetric split grid; React 18.3.1 → compatible) · **`shiki`** (VS Code-identical tokenization). **Hatching** (diagonal `repeating-linear-gradient`) fills the empty side of an unbalanced hunk so the reader's spatial anchor never breaks. All reds/greens/background bind to **`var(--vscode-diffEditor-insertedTextBackground` / `-removedTextBackground` / `--vscode-editor-background` / `--vscode-editor-font-family)`** so light/dark themes adjust automatically. **Bundle discipline (binding):** shiki is **dynamically imported** (off the critical path) and built with a **fine-grained core** loading only the languages/themes actually used; the 7.14.2 DoD measures the webview IIFE `dist/` delta. **Streaming discipline (binding):** never re-highlight or re-diff on a per-token basis — highlight on **block-complete**, diff on **edit-arrival** only. *Anti-patterns:* bundling full shiki with all grammars; re-tokenizing every streamed chunk; hard-coded hex reds/greens that ignore the theme.

### [ADR-723] Ghost Telemetry enhancement (additive over the mature surface)

Add, without replacing the existing chips/HUD: (a) colored **status dots** left of each action (blinking gray = running, solid green = done, red = error) on `ToolChip` headers; (b) a **live action-log** that streams tool invocations (`Read src/app.ts`, `Bash grep …`) in tiny muted font *while the agent is thinking* (extend `PipelineProgress` or a new `ActionLog`); (c) a per-message **bottom status bar** with a **live token counter** during streaming ("Thinking… N tokens" / playful "Reticulating…") — today only a *final* count exists, so the live counter is the one genuinely new datum. *Anti-patterns:* a second telemetry HUD; replacing the OCC/TPS/FinOps card; a status bar that steals focus from the code.

### [ADR-724] Inline per-diff HITL — reuse the existing approval channel

Under each `DiffBlock`, render discrete `[✓ Accept] [✗ Reject] [💬 Comment]`. **Reject/Comment** opens a **nested micro-input beneath that diff** and **must not clear** the main composer draft (rapid-iteration flow). When a diff is focused: `Ctrl+Enter` = Accept, `Esc` = Reject. These map to the existing `HITL_RESPONSE` (`approved` / `comment` / `modified_content`) — **no new event**. **Honest scope note (binding):** approval today is **per-patch** (one card, one `approval_id`), not per-hunk; 7.14.4 co-locates the existing per-patch decision under its diff and defers true **per-hunk `approval_id`s** (a backend change) to a later phase. *Anti-patterns:* a new approval event; clearing the draft on reject; blocking the whole panel for one diff.

### [ADR-725] Procedural-memory surfacing — inline Revert reuses branch-from-checkpoint

Messages carrying `checkpoint_id` get an inline circular **Revert ("rewind to here")** affordance that fires the existing `BRANCH_FROM_CHECKPOINT` → `client_branch_from_checkpoint` flow — time-travel without opening the picker. @-mentions are mature; only minor polish is in scope (folder-oversize toast surfaced to the panel; `@terminal` stub honesty). *Anti-patterns:* a parallel checkpoint store; rebuilding the mention trie.

### [ADR-726] Elite-gaps slice — in-scope additions vs. Phase-11 deferrals

**In scope (cheap, high-value):** (a) a **context-budget meter** ("N tokens left / context X% full") driven by the backend's existing `token_usage` + `context_window`; (b) an **auto-accept edits** toggle (soft permissions for fast repetitive flows) that bypasses the HITL card for low-risk edits. **Deferred to Phase 11 (Portfolio) with rationale:** parallel multi-thread runs, cross-session context references, dual-mode (GUI/CLI) — each is an architecture program, not UI polish, and belongs to the standout-release phase. *Anti-patterns:* sneaking a multi-thread runtime into a "UI" phase.

---

## 3. Work Breakdown Structure (build order)

Stack/conventions first, then the lowest-risk net-new (canvas), then the crown jewel (diff), then the three enhancements that layer on the diff/canvas, then the gate.

### 7.14.0 — Stack, Theming & Conventions *(contract sub-phase, no UI)* — **[ADR-720..726]**
Pin libraries (`diff`, `react-diff-viewer-continued`, `shiki`), the `var(--vscode-*)` theming contract, the shiki **lazy-load / fine-grained-core** rule, and the **no-per-token-rehighlight** streaming rule. Produces no runtime change beyond `package.json` deps (added when 7.14.2 lands) and a short `docs`/code-comment contract.
- **DoD:** blueprint ADRs ratified; deps chosen and license-checked; bundle-budget target stated (webview IIFE delta ceiling).

### 7.14.1 — The Infinite Canvas (Zero-Bubble) *(NET-NEW · lowest risk · recommended first slice)* — **[ADR-720]**
- Strip `.ws-msg` bubble styling (border, radius, `max-width:88%`, role bg, `align-self`); content spans 100% width; widen/remove the centered `720/676px` caps so code breathes on panel resize.
- Hairline turn separators; subtle per-turn role label/icon; dual-density typography (prose `line-height` up, code compact).
- **Files:** `workspace.css` (`.ws-msg*`, `.ws-messages`, `.ws-main-left`, `.ws-bottom` widths, `.ws-md-*`), `Workspace.tsx` (role-label markup). **Reuse:** `MarkdownRenderer` unchanged.
- **DoD:** `npm run compile` + `npm run lint` 0; full-width verified at min & maximized panel; user/assistant legible by label alone; no bubble chrome remains; existing streaming cursor + thought box + tool chips still render.

### 7.14.2 — Elite Diff Engine (Split-Diff + Hatching + Contextual Header) *(NET-NEW · crown jewel)* — **[ADR-721, ADR-722]**
- Host: enrich the `server_apply_workspace_edit` handler to post `RENDER_DIFF {patch_id,file_path,old_content,new_content,status}` to the webview (old content from `PatchActuator`'s existing read). **No Python change.**
- New `DiffBlock.tsx`: `react-diff-viewer-continued` split grid; `jsdiff` math; `shiki` tokens (lazy, theme-bound); **hatching** on unbalanced hunks; **rigid contextual header** attached to the top edge (green/gray `Edit`/`Read` badge + monospace file path); rendered **inline** in the transcript where the edit is explained.
- Large-diff guard: collapse hunks beyond a line cap with "show more"; LF-normalize; cp1252-safe.
- **Files:** `workspace_panel.ts` (host enrich), `DiffBlock.tsx` (new), `Workspace.tsx` (render `RENDER_DIFF` in transcript), `workspace.css`, `package.json` (+3 deps). **Reuse:** the `StagingArea` Monaco approach as a *reference*, not a dependency.
- **DoD:** compile/lint 0; a real multi-file edit renders inline as split-diff with header + hatching; light↔dark theme flips colors via CSS vars; a 2k-line file diff does not freeze the panel; **webview IIFE bundle delta within the 7.14.0 budget**; WebView teardown mid-render re-hydrates from the persisted payload.

### 7.14.3 — Ghost Telemetry (ENHANCE) — **[ADR-723]**
- Status dots on `ToolChip`; live action-log while thinking; per-message live-token footer.
- **Files:** `ToolChip.tsx`, `ThoughtBox.tsx` / new `ActionLog.tsx`, `thinkingReducer`, `Workspace.tsx` footer, `workspace.css`.
- **DoD:** compile/lint 0; dots track `pending→success/error`; action-log streams during a turn; token footer ticks **live** mid-stream and freezes on `server_stream_end`; no regression to the OCC/TPS/FinOps HUD.

### 7.14.4 — Inline per-diff HITL + keyboard (ENHANCE) — **[ADR-724]**
- Accept/Reject/Comment under each `DiffBlock`; nested re-prompt that preserves the draft; `Ctrl+Enter`/`Esc` on a focused diff. Reuse `HITL_RESPONSE`.
- **Files:** `DiffBlock.tsx` (+ actions), `Workspace.tsx` (wire to `HITL_RESPONSE`), `HITLInterventionCard.tsx` (co-locate pattern), `workspace.css`.
- **DoD:** compile/lint 0; Accept/Reject round-trips via the existing channel; Comment opens a nested input **without** clearing the composer draft; keyboard works on a focused diff; honest per-patch-vs-per-hunk note carried in code comment + journal.

### 7.14.5 — Procedural Memory surfacing (SURFACE/ENHANCE) — **[ADR-725]**
- Inline circular Revert on `checkpoint_id` messages → existing branch flow; @-mention polish (folder-oversize toast; `@terminal` honesty).
- **Files:** `Workspace.tsx` / `MessageActions`, `workspace.css`; minor `PromptBar.tsx` / `workspace_panel.ts`.
- **DoD:** compile/lint 0; Revert branches from that checkpoint (no picker); mention polish does not regress the trie autocomplete.

### 7.14.6 — Elite Gaps (strategic-auditor additions) — **[ADR-726]**
- Context-budget meter (tokens left / context %); auto-accept edits toggle.
- **Files:** `TelemetryHUD.tsx` or message footer, a mode toggle in the HUD/ModeMenu, `Workspace.tsx`, `workspace.css`.
- **DoD:** compile/lint 0; budget meter reflects real `token_usage`/`context_window`; auto-accept applies low-risk edits without the HITL card and is clearly indicated; deferrals (multi-thread / cross-session / CLI) recorded as Phase-11 backlog.

### 7.14.7 — Checkpoint Gate Fase 7.14 *(project convention)*
- Per-epic DoD matrix. 7.14 is **almost entirely frontend** → certified by `npm run compile` + `npm run lint` + manual smoke (mirrors the 7.13 frontend-only gate rows). Add a sibling pytest gate **only** for any backend-assertable row — here the sole candidate is the **host-enrichment contract** (ADR-721), which is host-TS, so it stays in the frontend-smoke column. This gate's closure **expires the §1 LOCK-IN.**

---

## 4. Architectural Risks (CLAUDE.md E2E awareness — binding)

- **Webview bundle bloat:** shiki dynamic-imported, fine-grained core, restricted langs/themes; bundle delta measured in 7.14.2 DoD. A bloated IIFE bundle slows every panel open.
- **Streaming cost:** highlight on block-complete, diff on edit-arrival — never per token (CPU/jank on long generations).
- **Theme reactivity:** colors are CSS-var-driven so a VS Code theme switch re-paints without reload; shiki theme follows.
- **Large diffs / encoding:** collapse oversized hunks; LF-normalize (host already does for inline edits); guard cp1252 → UTF-8.
- **WebView teardown mid-stream:** diffs derive from persisted edit payloads (inflight-snapshot pattern), not transient React state.
- **No Python contract drift:** ADR-721 forbids `ws_contracts.py` changes; the diff source is host-side enrichment only.

---

## 5. Definition of Done (phase-level gate matrix — ratified at 7.14.7)

| Row | Invariant | Certified by |
|---|---|---|
| **ZB1** | No `.ws-msg` bubble chrome; transcript is 100%-width and grows on panel resize | npm compile + manual smoke |
| **ZB2** | User vs assistant legible by role label alone (no role backgrounds) | manual smoke |
| **DF1** | A real `ApplyWorkspaceEditPayload` renders inline as split-diff + contextual header + hatching | manual smoke (host enrich) |
| **DF2** | Diff colors flip on light↔dark theme via `var(--vscode-*)` | manual smoke |
| **DF3** | shiki lazy-loaded; webview IIFE bundle delta within the 7.14.0 budget | `dist/` size measure |
| **DF4** | No per-token re-highlight/re-diff; 2k-line diff stays responsive | manual smoke / profile |
| **GT1** | Tool status dots + live action-log + live-token footer; OCC/TPS/FinOps HUD intact | manual smoke |
| **HL1** | Per-diff Accept/Reject round-trips via existing `HITL_RESPONSE`; reject keeps draft; diff-focus keyboard works | manual smoke |
| **PM1** | Inline Revert branches from the message's checkpoint | manual smoke |
| **EG1** | Context-budget meter reflects real usage; auto-accept respects mode | manual smoke |
| **REG** | `npm run compile` 0 errors · `npm run lint` 0 errors · no Python contract change · backend `pytest` baseline unaffected | CI gate |

### 5.1 Closure
This section ticks to `[x]` only when every row above is certified. **At closure the §1 LOCK-IN expires.**

---

## 6. Out of scope / deferred (Phase 11 — Portfolio)
Parallel multi-thread runs · cross-session context references · dual-mode GUI/CLI · true per-hunk approval IDs (backend) · binary packaging. Recorded here so a future PR does not silently absorb them into a "UI" slice.

---

## 7. Successor Track (Phase 7.15) & Checkpoint Dependency

A pre-checkpoint technical audit found that 7.14 delivered the **frontend surfacing** faithfully — its ADRs (720–726) and DoD matrix (§5) remain valid as written — but the **backend does not yet honor several of the affordances the UI now surfaces**. Mode routing, the ⟲ Rewind affordance, the inline diff, and live token streaming are visible in the panel yet inert end-to-end.

**Single root cause.** `core/task_service.py::_run_coding_task` invokes the planner/coder nodes **directly as async functions**, never through the compiled LangGraph engine (`alienant_app`). One shortcut disables, at once: the mode router `route_after_summarize`, the Socratic `ideation_loop`, and the `HybridCheckpointer` (no checkpoint → no `checkpoint_id` → no Rewind glyph) — and forces a freeze-then-dump instead of a token stream. Orthogonal defects (RBAC engine built but unwired; Spanish system prompts with no language-mirroring; a stale "Applying changes to disk is not yet enabled" string that contradicts the working apply path) round out the audit.

**These are tracked in the new Phase 7.15 (Agentic Core Remediation, ADR-727..732), not here** — 7.14 stays scoped to the UI. Phase 7.15 **does** change the Python contract; that is expected and correct for a backend-correctness track and does **not** retroactively violate 7.14's ADR-721 (which governed the 7.14 UI slices only).

**Checkpoint dependency (binding).** The §5 gate **7.14.7 must not tick `[x]` until Phase 7.15's gate (7.15.7) is green**, certifying the live task path engages the compiled engine. Closing 7.14 first would ratify cosmetic affordances. **The §1 LOCK-IN therefore expires only when both 7.14.7 and 7.15.7 are certified.**

**Two deliberate non-regressions** (so a future reader does not refile them as 7.14 bugs):
- **No syntax highlighting in chat code blocks** — the known shiki deferral, tracked as **DEBT-006** (bundle ceiling, ADR-722). The deferral is now **owned by Phase 7.16** (static, host-delegated tokenization) and **Phase 7.17** (streaming render) — was previously "a future 7.14.x / Phase 11"; not a 7.14 regression.
- **Rich plan side-panel** — never in 7.14 scope; it is a genuine new feature, now owned by **7.15.6**, not a 7.14 regression.
