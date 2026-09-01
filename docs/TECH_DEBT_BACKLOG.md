# Tech Debt Backlog — Continuous Registry Protocol

## The Rule

If you discover a strict-mode error, vulnerability, or typing debt **outside the scope of the
current ticket/subfase**, you MUST:

1. **STOP** — do NOT fix it in-place.
2. **ADD an entry** to this file using the format below (reproduction command + file + context).
3. **CONTINUE** with the current task.

This ensures every fix is atomic, auditable, and in the correct topological order. In-place fixes
of out-of-scope debt create invisible changes that break reviewers' ability to verify the diff.

---

## Entry Format

```
### DEBT-NNN [TIER · Schedule] — Short description
- **Date:** YYYY-MM-DD
- **Reproduce:** exact shell command that surfaces the error
- **File(s):** affected path(s) and line numbers if known
- **Error:** mypy error code or free-text description
- **Blocked by:** external dependency / phase prerequisite (if any)
- **Phase:** which Phase 8 subfase will address this
- **Notes:** context for future reader
```

---

## Tier Definitions

```
[HIGH]      Bounded correctness failures, reliability risks, or security concerns —
            mitigation exists but root cause is unresolved.
[MEDIUM]    Architecture debt, performance issues, test infrastructure gaps, or feature gaps
            whose absence is user-visible but not safety-relevant.
[LOW]       Type hygiene, test polish, UX convenience, deferred features with no urgency.
[DECISION]  Architectural decision record — not a defect; no fix planned.
```

Note: `[CRITICAL]` (active security exposure with no mitigation) is reserved for future use.
No currently-open entry meets that threshold.

**Excluded from the open-defect count:** `[DECISION]`-tier entries (see **Decision Records**)
and every entry filed under **Capability Backlog** — a capability that was never built is not a
defect in what shipped. The open-defect count refers only to entries in the **Open Entries**
section.

## Schedule-State Legend

```
Locked      Assigned to a specific not-yet-shipped sub-phase (e.g., 8.8.5).
Floating    Named with a vague post-X timing; no concrete sub-phase ticket yet.
Unscheduled No phase assigned, or the named phase already shipped without closing this entry.
Blocked     Has an external dependency (upstream stubs, CI lane, coordinated migration).
Decision    Not a defect — see [DECISION] tier.
```

---

## Open Items Dashboard

| ID | Title (short) | Tier | Type | Target Phase | Schedule |
|---|---|---|---|---|---|
| DEBT-014 | brain/swarms.py NodeInputT 6 residual ignores | LOW | Type hygiene | LangGraph stubs | Blocked |
| DEBT-081 | Analyst context budget — empty-L4 under-fill RESOLVED 2026-09-01 (demand-aware split + probed window); Project-layer degrade still drops README+GraphRAG wholesale, L5 still truncates at one uniform ratio | MEDIUM | Architecture | future context slice | Floating |
| DEBT-098 | Single ProcessPoolExecutor shared across PPR/indexer/blast-radius — no priority lanes | MEDIUM | Performance | future performance slice | Floating |
| DEBT-104 | Tournament surface rollback does not delete a candidate's newly introduced paths (`push_vfs_to_surface` only writes) — harmless for the agentic cell's same-file candidates, but `run_tournament_from_dispatch` fans out heterogeneous candidates that can contaminate siblings + the winner restore. 8.15.5 wired tournament winner-selection via a lightweight score/ok selector (not the full MCTS+verify path), so the contamination surface is not yet live; full delete-not-in-base isolation still owed | LOW | Correctness / isolation | future dispatch-isolation slice | Floating |
| DEBT-105 | Dispatch cost is estimate-based and under-counts on two axes — the reserve estimate under-models output tokens + context growth (admission is lenient), and the worker `cost_usd`/commit path meters only the tool-loop, not the `answer_fn` synthesis call or an `actual > reserved` overage; `finops`/`check_governor` remain the hard ceiling. 8.15.5 wired reserve@origin/commit@synthesize (admission now live); the metering residue remains | LOW | Correctness / cost accounting | future gateway-usage wiring | Floating |
| DEBT-161 | ~419 phase/ADR references remain across ~174 production files (corrected count, was 471/130), never scrubbed by any Phase 8-12 pass | LOW | Documentation hygiene | future phase-reference scrub slice | Floating |
| DEBT-164 | `core/memory/semantic_memory.py`'s `numpy`/`pyarrow`/`pyarrow.compute` imports stay module-level top (only `lancedb`/`litellm` were deferred) — ~35 usages across the file, too invasive to defer safely in one pass | LOW | Startup latency | future import-latency follow-up | Floating |
| DEBT-186 | `CoderCompanionPayload`/`server_coder_companion`/`brain/coder_companion.py`/`CoderCompanionCard.tsx` keep their coding-specific names even though 13.0.7 generalized the companion to explain every scope (ideation/planning/healing too) — evaluated a rename to `AgentCompanion*` and deferred it (no functional gain, costs a §10 dual-emit window) | LOW | Naming / clarity | rename with a dual-emit window, opportunistic | Floating |
| DEBT-187 | The planning-scope companion emission always uses `attempt_ordinal=0` (`agents/planner.py`) — no state counter exists to distinguish a mid-task replan's plan from the first one, so two plans in one task would collide on the same `emission_id` and the second would silently replace the first in the frontend's append store instead of adding a new entry | LOW | Correctness gap (narrow) | future replan-counter slice, if replanning-within-a-task ships | Floating |
| DEBT-192 | Local-model calibration is recorded only from `ainvoke`, not the direct-BYOM streaming paths — deliberate, confirmed with the user | LOW | Declared tradeoff | revisit if chat streaming on slow hardware needs it | Floating |
| DEBT-193 | A local-model timeout (`litellm.exceptions.Timeout`) never triggers the connection-drop failover path — only a genuine connection drop or CUDA OOM does | LOW | Correctness gap | future local-timeout-failover slice | Floating |
| DEBT-195 | Other flat, hardware-blind timeout constants surveyed during DEBT-191's audit — mostly a different subsystem (benchmark harness); one latent unscaled path off the primary route | LOW | Robustness | future timeout-audit follow-up | Floating |
| DEBT-203 | No adaptive execution-depth decision exists — every coding turn runs researcher+planner+full WBS regardless of task triviality | MEDIUM | Architecture | future triviality-classifier slice | Floating |
| DEBT-204 | Output-budget-brief candidate directions deferred by measurement (§8.1 incremental plan materialization, §8.3 GBNF, §8.5 slim schema, §8.9 streaming+incremental parse) | LOW | Declared tradeoff | revisit if measurements change | Floating |
| DEBT-205 | `run_checks` (deep effort) executes only the mechanically-executable subset of a plan's own `checks`; non-command criteria are excluded, not silently passed | LOW | Correctness gap (declared MVP) | future checks-verification slice | Floating |
| DEBT-206 | Zero `server_activity_event` fired during an entire 12-minute planner window (OQ-6) — narration wiring gap, not yet investigated | LOW | Observability | short separate investigation | Floating |
| DEBT-221 | `run_agentic_cell_node` (`brain/agentic_cell.py:396`) is CC 108 (radon grade F) — the highest cyclomatic complexity in the backend | MEDIUM | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-222 | `run_planner_node` (`agents/planner.py:209`) is CC 84 (radon grade F) | MEDIUM | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-223 | `run_coder_node` (`agents/coder.py:459`) is CC 80 (radon grade F) | MEDIUM | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-224 | `run_researcher_node` (`agents/researcher.py:201`) is CC 76 (radon grade F) | MEDIUM | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-225 | `websocket_endpoint` (`main.py:1326`) is CC 59 (radon grade F) | MEDIUM | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-226 | `TaskService._format_coding_summary` (`core/task_service.py:1623`) is radon grade F | LOW | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-227 | `ValidateWBSDependenciesTool._arun` (`tools/planner_tools.py:116`) is CC 53 (radon grade F) | LOW | Architecture / maintainability | future node-decomposition slice | Floating |
| DEBT-230 | No gate validates compiled-graph integrity beyond conditional-edge path-maps — `Send()` targets unchecked; four dispatch nodes bypass `assert_declared_channels`; a computed router verdict is invisible to the static gate | MEDIUM | Verification coverage | future graph-integrity slice | Floating |
| DEBT-231 | `planner_retry_count` (4 writes, 0 readers) and `send_telemetry` (0 callers, so `routing_warning` never reaches the user) are dead signals no enabled lint rule can see | LOW | Dead code / observability | wire the consumer or delete the producer | Floating |
| DEBT-232 | Provider-side reasoning tokens are not budgeted on the strict-JSON `response_format` path — a model billing reasoning inside `completion_tokens` can exhaust `max_tokens` before the object closes | LOW | Correctness gap (mitigated) | future gateway thinking-budget slice | Floating |
| DEBT-233 | `_inject_reasoning_scaffold` imposes one fixed 4-beat template on every free-form call, so all reasoning across the app shares a shape | LOW | Output quality / uniformity | future gateway prompt slice | Floating |
| DEBT-234 | Injected `cell_reasoner`/`dispatch_*` seams declare no model, so a non-default injection is budgeted against the default tier's window | LOW | Architecture | unscheduled | Floating |

---

## Debt Ratio

Derived, not asserted — recomputed at each phase closure that meaningfully changes the Open Items
Dashboard, using the counts from that table (HIGH/MEDIUM/LOW only, per the exclusion rule above)
and `radon raw -s` SLOC over `ailienant-core` excluding `venv/` and `tests/`.

```
Debt Ratio = (HIGH×3 + MEDIUM×2 + LOW×1) / KLOC(production)
```

**Baseline (2026-08-28, ailienant-core only):** 0 HIGH, 3 MEDIUM, 13 LOW → weighted 19 · SLOC 40,446
(radon raw) → Debt Ratio ≈ 0.47 / KLOC.

**Recomputed (2026-08-31, ailienant-core only):** the DEBT-221–DEBT-227 batch (7 new entries — 5
MEDIUM, 2 LOW, filed for the backend's 7 radon-grade-F cyclomatic-complexity functions) moves the
Open Items Dashboard count to 0 HIGH, 8 MEDIUM, 15 LOW → weighted 31 · SLOC 40,844 (`radon raw -s`
over `ailienant-core`, excluding `venv/`, `tests/`, and cache directories) → **Debt Ratio ≈ 0.76 /
KLOC**. `ailienant-extension/src` is not yet included (no `radon`-equivalent SLOC pass wired for
TypeScript) — extending the ratio there is itself DEBT-166-shaped (advisory, not blocking) and not
worth a dedicated ticket until the number is tracked over more than one data point.

**Recomputed (2026-08-31, 8.22 closure):** DEBT-230–232 (1 MEDIUM, 2 LOW, filed for what the seam
audit deliberately did NOT fix) move the dashboard to 0 HIGH, 9 MEDIUM, 17 LOW → weighted 35 ·
SLOC 41,160 (`radon raw -s`, same exclusions) → **Debt Ratio ≈ 0.85 / KLOC**. The rise is logging,
not decay: all three items are pre-existing conditions this phase found and named rather than new
debt it introduced. Worth stating plainly, because it is the shape this metric is worst at — a
ledger over KNOWN debt cannot fall when an audit converts unknown defects into tracked ones, and
reading the increase as regression would penalize exactly the work that should be rewarded.

**Recomputed (2026-09-01, 8.23 closure):** DEBT-233 (1 LOW — the gateway's global reasoning
scaffold, left deliberately out of scope) moves the dashboard to 0 HIGH, 9 MEDIUM, 18 LOW →
weighted 36 · SLOC 41,337 (`radon raw -s`, same exclusions) → **Debt Ratio ≈ 0.87 / KLOC**.
Essentially flat against 8.22's 0.85: this division removed the analyst's own fixed reasoning
template and logged the one remaining below it, so the ratio moved by a single LOW item on a
slightly larger codebase.

---

## Decision Records Dashboard

| ID | Title | Tier |
|---|---|---|
| DEBT-010 | OCC version-vectors on the graph state dict: rejected in favor of existing reducers (decision record) | DECISION |
| DEBT-102 | `tree-sitter-dart` single-release supply-chain risk | DECISION |
| DEBT-109 | Context-utilization telemetry is flat pipe-delimited text, not typed JSONL | DECISION |
| DEBT-131 | 7 tools deliberately left unwired in `core/tool_registry.py` (decision record) | DECISION · re-audited 2026-08-31 |
| DEBT-145 | Per-task reasoning-mode config rides mutable graph state, not a config table | DECISION |
| DEBT-149 | CSS's semantic-similarity term is deliberately calibrated against file-centroid distances only | DECISION |
| DEBT-159 | Pre-commit's mypy-on-changed-files is a local approximation only | DECISION |
| DEBT-165 | OpenSpec adoption is new-phases-only (decision record) | DECISION |
| DEBT-166 | `openspec validate` starts advisory, not blocking (decision record) | DECISION |
| DEBT-167 | OpenSpec CLI installed at repo-root `package.json`, not `ailienant-extension/package.json` (decision record) | DECISION |
| DEBT-177 | Three declared conservatisms in the tool-selection path | DECISION |
| DEBT-198 | `pre_patch` hooks now run once per WBS step, not once per turn | DECISION |
| DEBT-208 | Deleting the dead topology-selector modules is a permanent test-coverage reduction, not a gap to backfill | DECISION |

---

## Capability Backlog Dashboard

| ID | Title | Tier |
|---|---|---|
| DEBT-025 | Docker persistent-PTY backend has no daemon integration test | LOW · Blocked |
| DEBT-209 | No way to change the LLM while a task is already running | MEDIUM · Floating |
| DEBT-210 | No automatic subsystem/community detection in internal GraphRAG | MEDIUM · Floating |
| DEBT-211 | Internal GraphRAG has no git/PR-history awareness | LOW · Floating |
| DEBT-212 | GraphRAG and project docs are separate context sources, never nodes in one graph | MEDIUM · Floating |
| DEBT-213 | `web_fetch` destination guard is open to DNS rebinding (double resolution) | MEDIUM · **RESOLVED 2026-08-31, 8.20** |
| DEBT-214 | DuckDuckGo search fallback parses an unversioned public HTML page | LOW · Floating |
| DEBT-215 | `ROLE_REGISTRY.allowed_tools` is NOT vestigial — it recorded the contract live RBAC had drifted from | LOW · **RESOLVED 2026-08-31, 8.20** (premise reversed) |
| DEBT-035 | MultiPL-E TypeScript execution needs a Node-capable sandbox runtime | MEDIUM · Floating |
| DEBT-074 | `pre_file_read` GraphRAG-injection hook bypasses cost accounting | MEDIUM · Blocked |
| DEBT-075 | Syntactic-only symbol extraction; no LSP-style type resolution | LOW · Unscheduled |
| DEBT-087 | Python relative imports skipped by the extractor | LOW · Floating |
| DEBT-088 | `bfs_k_hop_backward` has the pre-8.14.1 resolved-form gap | LOW · Floating |
| DEBT-089 | Blast-radius Python resolution is suffix-based, not sys.path-aware | LOW · Floating |
| DEBT-090 | Memory-snapshot export has no extension-side trigger | LOW · Floating |
| DEBT-091 | Architecture digest omits git co-change coupling | LOW · Floating |
| DEBT-092 | Boundary graph cannot recover backend `server_*` emit edges | LOW · Floating |
| DEBT-093 | Boundary graph has no auto-refresh on index-complete | LOW · Floating |
| DEBT-095 | Polyglot (TS/JS) runtime call-trace capture | LOW · Floating |
| DEBT-096 | Sandbox/agentic-cell-integrated live trace capture | LOW · Floating |
| DEBT-101 | Observed-call-edge substrate has no purge/TTL | LOW · Floating |
| DEBT-103 | Dart `package:` URI resolution is pubspec-unaware | LOW · Floating |
| DEBT-107 | Autonomous LLM-driven `DispatchPlan` emission is deferred | MEDIUM · Floating |
| DEBT-111 | GraphRAG nebula limited to file/external node types | MEDIUM · Floating |
| DEBT-113 | Nebula picking + layout not yet scaled to 100k nodes | LOW · Floating |
| DEBT-114 | Search pulse is not a real GraphRAG reasoning-path replay | LOW · Floating |
| DEBT-216 | No rolled-up agentic product metrics (task completion, tool-call accuracy, cost/task, self-correction rate) | MEDIUM · Floating |
| DEBT-115 | Per-project token-cost bucketing deferred from 11.1 | LOW · Floating |
| DEBT-135 | Playwright dashboard fixture bypasses the real indexer | LOW · Floating |
| DEBT-136 | Playwright suite is Chromium-only, no cross-browser matrix | LOW · Floating |
| DEBT-137 | Provider-native `cache_control` + cache telemetry not implemented | LOW · Floating |
| DEBT-138 | Agentic cell does not route through the devcontainer session tier | MEDIUM · Blocked |
| DEBT-139 | Devcontainer session host driver has no real TTY | LOW · Floating |
| DEBT-148 | Dashboard vector scatter map surfaces only file-level embeddings | LOW · Floating |
| DEBT-154 | Apply-edge risk gate is still a command-pattern proxy, not a real edit-risk classifier | LOW · Floating |
| DEBT-155 | File-read content preview not on the Glass-Box Timeline | LOW · Floating |
| DEBT-156 | No automated CLA-assistant workflow | LOW · Floating |
| DEBT-157 | No unit/integration/e2e taxonomy across the backend test suite | MEDIUM · Floating |
| DEBT-158 | Playwright e2e coverage is a single 4-case Dashboard-only spec | MEDIUM · Floating |
| DEBT-169 | GraphRAG/tool retrieval has no reranking stage | MEDIUM · Floating |
| DEBT-174 | Coder-node edit generation never receives image attachments | LOW · Floating |
| DEBT-175 | `TOOL_RAG_TOP_K` cannot rise until the Phase-5.7 gate's baseline is reworked; its prescribed remedy is near self-cancelling | MEDIUM · Floating |
| DEBT-176 | No tool-invocation telemetry exists; a usage prior would break `select_tools` determinism | LOW · **RESOLVED 2026-08-31, 8.20** (emit-only half was already shipped) |
| DEBT-178 | `toggle_plan_mode`'s READ_ONLY tier cannot express that it mutates the permission channel | LOW · Floating |
| DEBT-194 | No liveness signal exists to distinguish "local model is slow" from "local model is dead" | LOW · Floating, PARTIALLY RESOLVED 2026-08-31 (streaming paths only) |
| DEBT-199 | `apply_patch`/`apply_commit` assume SWARM (`parallel_tasks`) stays dormant | LOW · Floating |
| DEBT-200 | No one-click revert for an applied step; VS Code Local History is the only recovery path | MEDIUM · Floating |
| DEBT-217 | No Runtime Capacity panel; the chat HUD's context-window ring reads the wrong denominator on a local target | MEDIUM · Floating |
| DEBT-218 | No way to reconfigure Ollama's KV-cache quantization from AILIENANT | MEDIUM · Floating |
| DEBT-219 | `batch_semantic_edit` (multi-file ACID) stays excluded for want of a safe `vfs_write` closure | MEDIUM · Floating |
| DEBT-220 | `bind_cell_tools` has no consumer and would advertise names the dispatcher does not match | LOW · Floating |
| DEBT-228 | Six gateway tests read the host's live free RAM and fail on a memory-starved machine | MEDIUM · Floating |
| DEBT-229 | Mid-run steering reaches the one-shot coder path only at a node boundary, not per iteration | LOW · Floating |

---

## Open Entries

---

**HIGH**

---

### DEBT-168 [HIGH · RESOLVED 2026-08-17, 13.0.1] — Image attachments never reached the LLM's multimodal payload

- **Date:** 2026-08-17 · **Resolved:** 2026-08-17 (13.0.1)
- **Was:** attaching an image and submitting a coding task correctly flipped `has_images` (`core/task_service.py::_build_initial_state`), but nothing forwarded it. **Correction to the original entry's premise:** it claimed "the routing half of Vision Bypass shipped" — verified false during the fix. `RoutingEngine.resolve_provider`/`get_optimal_provider` (`brain/routing_engine.py`) have zero production callers; the live routing cascade is `core/memory/context_auditor.py::derive_routing_decision`, which has no `has_images` parameter at all. Both the routing half and the payload half were unwired.
- **Resolution:** `agents/researcher.py`'s cascade gained a Vision Bypass override — after the cascade settles and before the hardware-reroute block, `state["has_images"]` forces `routing="CLOUD"`, warning (not silently degrading) when no cloud provider is configured. `LLMGateway.ainvoke` (`tools/llm_gateway.py`) gained an additive `images: Optional[List[Dict[str, str]]] = None` parameter; a new `_attach_images_to_messages` helper runs against the *physically resolved* model (post BYOM-alias resolution, since the caller-supplied alias tells nothing about vision capability) and gates on `litellm.supports_vision()` — already vendored, pure, no network. A `False` verdict (which includes most local BYOM targets, since they're absent from litellm's model map) logs a WARNING rather than silently dropping the attachment, since `supports_vision` returning `False` for an unknown model is indistinguishable from a genuine non-vision model without the warning. Count/size ceilings (`AILIENANT_VISION_MAX_IMAGES_PER_CALL`, `AILIENANT_VISION_MAX_TOTAL_BASE64_CHARS`, `shared/config.py`) are checked via `len()` before any content block is built (§5.5) — deliberately not offloaded to `asyncio.to_thread`, since the work is GIL-bound string/dict construction that a thread hop wouldn't actually parallelize. The researcher's own skeleton call is the wiring: `agents/researcher.py` builds `images=` from `state["attachments"]` filtered to `type == "image"`.
- **Scope boundary:** only the researcher's answer call was wired — it is the node that owns comprehension of the request and seeds everything downstream. The coder's separate edit-generation call never sees attachments; re-logged as **DEBT-174**.
- **Files:** `agents/researcher.py`, `tools/llm_gateway.py`, `shared/config.py`.
- **Verified:** `tests/test_vision_payload.py` (9 cases: content-block shape, non-vision passthrough+warning, count/size ceilings, routing override, cloud-unavailable warning); mypy 0; pyright 0.

### DEBT-170 [HIGH · RESOLVED 2026-08-17, 13.0.1] — Resubmitting a prompt mid-run could spawn a second concurrent runner on the same checkpoint

- **Date:** 2026-08-17 · **Resolved:** 2026-08-17 (13.0.1)
- **Was:** `submit_task` (`main.py`) had no concurrency guard per session — a resubmit against the same `session_id` before the first turn finished spawned a second `asyncio.create_task(_runner())` unconditionally, both running against the same LangGraph checkpoint. `PromptBar.tsx`'s submit path only checked `disabled` (HITL-pending), not `isStreaming`.
- **Resolution:** policy is reject, not queue or interrupt-and-replace — Stop/Esc already gives an explicit, user-initiated replace path via `abort_session`. New `TaskService.is_session_busy(session_id)` is true for a live not-`done()` runner task *or* an unabandoned HITL pause; `submit_task` rejects with a new additive `"busy"` status (mirroring `submit_benchmark`'s existing precedent) before spawning. `register_active_task`'s documented replace semantics are untouched — the guard lives strictly upstream in `submit_task`. `_paused_tasks` now carries a `paused_at` timestamp; a pause older than the new `AILIENANT_PAUSED_INTERRUPT_TTL_S` knob (default 6h) is treated as abandoned and discarded by `is_session_busy` itself, so a dismissed or lost HITL card can never wedge a session "busy" for the process lifetime — a genuinely late reply still resolves safely through `resume_graph`'s existing no-such-entry no-op. `_active_tasks` needed no TTL: its done-callback fires on every terminal path (return, exception, cancel), so a lingering entry is already inert against the `task.done()` check. `PromptBar.tsx`'s `submit()` now also gates on `isStreaming`.
- **Files:** `core/task_service.py`, `main.py`, `shared/config.py`, `ailienant-extension/src/workspace/components/PromptBar.tsx`.
- **Verified:** `tests/test_session_admission.py` (7 cases, incl. abandoned-pause reclamation and an explicit `register_active_task` replace-semantics regression row); mypy 0; pyright 0.

### DEBT-171 [HIGH · RESOLVED 2026-08-17, 13.0.1] — `AskUserQuestionTool` wrote a state channel that nothing consumed

- **Date:** 2026-08-17 · **Resolved:** 2026-08-17 (13.0.1)
- **Was:** `AskUserQuestionTool._arun` wrote `state["pending_hitl_request"]` but no node read it — the tool's own docstring claimed an "orchestrator graph node" consumer that did not exist.
- **Resolution — could not be "repoint `_arun` at `request_graph_clarification`" as originally framed:** `interrupt()` cannot run safely mid-tool-dispatch-loop — a resume would replay every side effect already committed that super-step, the exact hazard `pending_tool_call` (DEBT-129) exists to avoid. `brain/agentic_cell.py` gained the same defer-then-interrupt-first two-phase shape: the registry-fallback loop detects a populated `pending_hitl_request` right after dispatch (the tool is READ_ONLY, so `classify()` never routes it to the existing HITL-defer branch), pops it, and returns immediately — stopping further calls that super-step so nothing after it runs before the human answers, while a preceding sibling's mutations (already in `vfs_buffer`/`pending_contents`) ride out unchanged. A new clarification-resume phase on the next iteration calls `request_graph_clarification()` as its first action and folds the answer into the trajectory. `agents/coder.py`'s READ_ONLY grounding pre-pass — which has no resume phase and is re-entered by the retry loop — excludes the tool outright.
- **Two additional bugs found and fixed, not named by the original entry:** (1) `task_service.py::_emit_interrupt_card` built the outbound HITL card from a hardcoded `action_description` fallback and dropped `question`/`context`/`suggested_options` entirely — the ambiguity gate and Plan-mode suggestion shipped the same morning (`agents/researcher.py`) were rendering a card the user couldn't actually read. (2) `_emit_interrupt_card` also only read `approval_id` for the card's correlation id, but `request_graph_clarification`'s payload is keyed on `request_id` — a clarification's card correlation id resolved to the empty string. Both fixed via additive fallbacks (`approval_id or request_id`, `action_description or question`) plus additive `question`/`context`/`suggested_options` fields on `HITLApprovalRequestPayload`. Inbound, `client_hitl_response`'s resume value was hardcoded to `{approved, comment}`, so `request_graph_clarification` always saw `answer=None` — fixed with additive `answer`/`selected_option` fields on `HITLResponsePayload`, extracted into a testable `main.py::_resume_approval_dict` that falls `answer` back to the existing free-text `comment` field so the already-shipped ambiguity gate works against today's unchanged card with zero frontend changes.
- **Files:** `brain/agentic_cell.py`, `agents/coder.py`, `api/ws_contracts.py`, `core/task_service.py`, `main.py`, `tools/control_tools.py`, `tools/agent_tools.py`, `docs/SYSTEM_PROMPTS.md` (docstring scrub, §13).
- **Verified:** `tests/test_clarification_channel.py` (9 cases: defer + sibling-integrity, resume-phase interrupt-first, grounding-loop exclusion, outbound card correlation/question forwarding for both interrupt shapes, inbound answer/comment fallback); `SCHEMA_EVOLUTION.MD §51`; mypy 0; pyright 0.
- **Notes:** DEBT-172 narrows to just the FE multi-choice renderer — the channel itself now round-trips correctly through the existing plain approve/reject card's comment box.

### DEBT-172 [LOW · RESOLVED 2026-08-18, 13.0.4] — Clarification / Plan-mode-suggestion interrupt cards had no multi-choice renderer

- **Date:** 2026-08-17 · **Resolved:** 2026-08-18 (13.0.4)
- **Was:** the wire payload carried `question`/`context`/`suggested_options` (DEBT-171), but two independent gaps meant nothing ever showed options: `ailienant-extension/src/api/contracts.ts`'s `HITLApprovalRequestPayload` never declared those fields at all (they arrived over the wire and were silently dropped), and `HITLInterventionCard.tsx` only ever rendered a Cancel/Modify/Authorize approve-reject row — plus `ask_user_question`'s own schema gave the LLM no structured way to express more than one concrete option, so in practice it tended to emit at most one bare "recommended" suggestion.
- **Resolution:** closed both halves together. Frontend: `contracts.ts` gained the missing fields plus a new additive `questions` batch field; a new `ClarificationGrillCard.tsx` renders a tab strip (multi-question) or single options panel with a "(Recommended)" badge and an implicit free-text "Other" row per question, normalizing the legacy single-question shape into a one-item batch so the two existing internal callers (ambiguity gate, Plan-mode suggestion) are upgraded for free; `Workspace.tsx` branches to it whenever `question`/`questions` is present, leaving every other HITL kind on the unchanged approve/reject card. Backend: `AskUserQuestionInput` (`tools/control_tools.py`) gained an additive `questions` field whose descriptions explicitly instruct 2-4 concrete mutually-exclusive options per question with exactly one `recommended`; `core/hitl.py::request_graph_clarification`, `task_service.py::_emit_interrupt_card`, `brain/agentic_cell.py::_resolve_pending_clarification`, and `main.py::_resume_approval_dict` all forward the new `questions`/`answers` fields additively (`SCHEMA_EVOLUTION.MD §52`).
- **Files:** `api/ws_contracts.py`, `tools/control_tools.py`, `core/hitl.py`, `core/task_service.py`, `brain/agentic_cell.py`, `main.py`, `ailienant-extension/src/api/contracts.ts`, `ailienant-extension/src/workspace/components/ClarificationGrillCard.tsx`, `ailienant-extension/src/workspace/components/HITLInterventionCard.tsx`, `ailienant-extension/src/workspace/utils/clarificationLogic.ts`, `ailienant-extension/src/workspace/utils/useClarificationResponder.ts`, `ailienant-extension/src/workspace/Workspace.tsx`, `ailienant-extension/src/workspace/workspace.css`.
- **Verified:** backend — extended `tests/test_control_tools.py` (batch mode + required-field guard) and `tests/test_clarification_channel.py` (outbound batch forwarding + synthesized description, resume-phase multi-answer trajectory fold, inbound `answers` forwarding); frontend — new `src/test/clarificationGrillCard.test.ts` (7 cases against the pure `clarificationLogic.ts` module, since this suite has no DOM-render harness); full suites green: mypy 0/468, pytest 2979 passed/2 skipped, npm compile/lint 0, mocha 198 passed.


### DEBT-036 [HIGH · RESOLVED 2026-06-19, 8.10.5] — BenchmarkOracle executed candidate patches on the host (no sandbox isolation)

- **Date:** 2026-06-12 · **Resolved:** 2026-06-19 (8.10.5)
- **Was:** `BenchmarkOracle.run_oracle` assembled the workspace in a host `TemporaryDirectory` and emitted
  `sys.path.insert(0, <host tmpdir>)`; that host path is invisible inside the Docker container, so the
  multi-file oracle never actually isolated live model output.
- **Resolution:** workspace materialization moved into the executor via an additive `CodegenExecutor.run_workspace`.
  `SandboxCodegenExecutor.run_workspace` materializes the corpus snapshot + patch under the active
  `DockerSandboxAdapter.host_workspace` mount (the single mount authority) and runs `python3 __oracle_main__.py`
  with `cwd` = that dir, so Python puts the dir on `sys.path[0]` (no absolute path embedded; host/container
  parity). Isolation: ro mount, `--network none`, env-whitelist `{PYTHONDONTWRITEBYTECODE=1}` (no root-owned
  `__pycache__`), `rmtree(ignore_errors=True)` cleanup. The hermetic gate keeps `SubprocessPythonExecutor`
  (trusted fixtures). Patch path-traversal is a strictly lexical pre-I/O guard (`_safe_relative`).
- **Files:** `core/benchmark/executors.py`, `core/benchmark/oracle.py`, `core/sandbox.py`.
- **Verified:** `tests/benchmark/test_oracle_resolve_k.py` (sandbox-routing + lexical-guard rows); mypy 0; full suite green.

### DEBT-013 [HIGH · RESOLVED 2026-06-19, 8.10.5] — Thinking-stream coding turns dropped hard JSON-mode (`response_format`)

- **Date:** 2026-06-05 · **Resolved:** 2026-06-19 (8.10.5)
- **Was:** on a native-thinking model with thinking ON, `acomplete_with_thinking` took the streaming branch,
  which could not pass `response_format`; planner/coder JSON was only prompt-enforced + sanitizer-recovered.
- **Resolution:** `astream_byom_thinking` gained an optional `response_format`; `acomplete_with_thinking`
  forwards it on the streaming branch only when `_supports_streaming_structured_output(target)` (default-deny
  provider allowlist, currently `{openai}`). A backend that rejects the param degrades once — memoized in
  `_RESPONSE_FORMAT_UNSUPPORTED` and retried without it before any chunk is consumed (mirrors `ainvoke`). The
  ADR-742 sanitizer stays the universal fallback, so incapable providers (Anthropic, local reasoners) are
  unchanged. The `{openai}` frozenset is the single tuning point as providers are verified.
- **Files:** `ailienant-core/tools/llm_gateway.py`.
- **Verified:** `tests/test_streaming_structured_output.py` (forward / drop+sanitize / degrade+memo / pre-strip);
  existing 7.17 streaming + response-format suites green; mypy 0.

### DEBT-097 [HIGH · RESOLVED 2026-08-03, 12.6] — Single shared Docker sandbox container across all concurrent sessions

- **Date:** 2026-07-02 · **Resolved:** 2026-08-03 (12.6)
- **Was:** `resolve_default_adapter()` bound one module-level `ACTIVE_ADAPTER`, and `DockerSandboxAdapter.__init__` held one `self._container` reused by every `execute()`/`open_session()` call for the lifetime of the process, regardless of which session or project issued it — one CPU/memory ceiling and one 512 MB `/work` tmpfs shared by every concurrent session, plus a fixed read-only mount bound to `os.getcwd()` at construction time (a session against a second project silently fell back to the first project's `/workspace`).
- **Resolution:** `DockerSandboxAdapter` now leases containers from a bounded `_ContainerPool` keyed by `(abspath(mount_root), session_id or "__shared__")` — concurrent sessions, and concurrent projects, get distinct containers instead of contending for one. `mount_root` resolves per session through an additive DI seam (`set_session_workspace_resolver`, mirroring the `set_trusted_bridge` precedent) that `main.py` wires to the existing `_session_workspace_root` registry; a resolver miss falls back to the adapter's own `host_workspace`, so every caller with no session (the untrusted benchmark oracle, hook execution) is behavior-unchanged. At capacity: an idle (refcount-0) lease is LRU-evicted first; if none is idle, acquisition waits up to `AILIENANT_SANDBOX_LEASE_WAIT_S` (30s) for a release, then shares the LRU lease **only if it is mounted at the same root** — sharing across mount roots is refused outright (`SandboxResourceExhausted` → `[sandbox_pool_exhausted]`), since that would silently execute a command against the wrong project rather than merely lose CPU/RAM isolation. Per-container `mem_limit`/`pids_limit` ceilings bound the noisy-neighbor half directly (no CPU ceiling — would distort the benchmark oracle). Interactive PTY sessions release their lease exactly once via an idempotent `on_close` callback threaded through `_DockerPtyBackend`. Startup reclamation (`sweep_orphaned_containers`) removes containers left by a crashed prior run or the old singleton, gated on a TCP liveness probe of the owning port (mirroring `core/config/host_discovery.py`'s own discipline) so a **live sibling backend's** containers — the extension spawns one backend per VS Code window on a dynamic port — are never touched; lifespan shutdown drains the whole pool.
- **Files:** `core/sandbox.py` (`_ContainerLease`/`_ContainerPool`/`sweep_orphaned_containers`), `main.py` (DI wiring + startup sweep + shutdown drain), `api/runtime.py` (`container_running` now pool-aware; additive `container_count`/`daemon_degraded`), `shared/config.py` (new knobs).
- **Verified:** `tests/test_sandbox_pool_resilience.py` (POOL1-9, PTY1); full suite green; mypy 0; pyright 0.

### DEBT-099 [HIGH · RESOLVED 2026-07-03, 8.15.0.1] — No client-side concurrency throttle on LLM Gateway calls

- **Date:** 2026-07-02 · **Resolved:** 2026-07-03 (8.15.0.1)
- **Reproduce (original):** `grep -niE "semaphore|rate.?limit|max_connections" ailienant-core/tools/llm_gateway.py` — no matches. The only admission control on an outbound LLM call was the ledger's **budget** check (`gateway/ledger.py`, dollars), not a **concurrency** limit (number of simultaneous in-flight calls).
- **Error (original):** reliability risk under fan-out. Team Swarms (concurrent `CoderAgent` clones via `Send()`) and the upcoming Division 8.15 (Dynamic Subagent Dispatch) could issue N simultaneous calls through the local LiteLLM proxy with no client-side backpressure — the failure mode discovered externally (a cloud provider's rate limit rejects requests) rather than prevented by AILIENANT's own admission control.
- **Resolved:** a per-event-loop `asyncio.Semaphore` (`tools/llm_gateway.py::_llm_semaphore`, keyed by the running loop via `WeakKeyDictionary`) now gates the five direct-call methods (`ainvoke`, `astream`, `acomplete_byom`, `astream_byom`, `astream_byom_thinking`), sized by new `AILIENANT_LLM_MAX_CONCURRENCY` (default 8, floored at 1; `shared/config.py`). The slot is held for the whole call — for streams, the whole stream — and released on every exit path (normal/exception/cancel) via `async with`. Delegating methods (`acomplete_with_thinking`, `ainvoke_by_priority`) and the private `_oom_cascade` never re-acquire (one slot per logical op); sync `invoke` is out of scope (asyncio primitive can't gate blocking code — no production callers) and carries a bypass-DANGER docstring warning.
- **Key decision:** a **dedicated** env var, NOT a reuse of `AILIENANT_MAX_CONCURRENT_SUBAGENTS` (which the 8.15 blueprint defines as a *plan-time* wave-split ceiling, explicitly "not a runtime semaphore"). This is a transport-layer runtime gate — the two enforcement layers stay distinct. Head-of-line blocking under consumer backpressure is an accepted tradeoff (true in-flight accounting); load-shedding via acquire-timeout is deferred.
- **File(s):** `tools/llm_gateway.py`, `shared/config.py`; gate `tests/test_phase8_15_0_1_checkpoint_gate.py` (THROTTLE1-5).
- **Notes:** carved as a pre-requisite for Division 8.15 (landed before 8.15.1's first concurrent caller), similar in spirit to how DEBT-069 (Researcher node promotion) was carved as a prerequisite before dispatch-loop work began.

### DEBT-100 [HIGH · RESOLVED 2026-08-03, 12.6] — Docker daemon hang blocks the sandbox worker thread indefinitely

- **Date:** 2026-07-02 · **Resolved:** 2026-08-03 (12.6)
- **Was:** every Docker SDK call ran synchronously inside a bare `asyncio.to_thread`, with no timeout at any layer. A hung **daemon** (as opposed to a hung in-container command, already bounded by the GNU `timeout` wrapper) parked the worker thread forever, and since `asyncio.to_thread` uses the interpreter's *shared default executor*, repeated hangs would eventually starve every other `to_thread` consumer in the process (janitor, PPR, indexer, blast-radius).
- **Resolution:** two layers. (1) **Socket-level timeouts** — every Docker client is now constructed with an explicit `timeout=` (verified against docker-py 7.1.0's `APIClient(timeout=...)`), so an unresponsive daemon surfaces as `requests.exceptions.ReadTimeout`/`ConnectionError` on the worker thread, which then returns to the pool in O(1) — no orphaned thread for the vast majority of calls. Since `exec_create`/`exec_start` accept no per-call timeout and `exec_run` blocks until completion, one-shot `execute()` calls resolve a client scoped to that call's own budget (bucketed to the nearest 30s, LRU-cached at 8 entries) rather than sharing the short-lived lifecycle client. (2) **Dedicated `ail-docker` `ThreadPoolExecutor` + a 3-state circuit breaker** (`core/sandbox.py::_docker_call`/`_DaemonBreaker`) as defense-in-depth for the one call socket timeouts cannot bound — a hijacked interactive-PTY exec socket (`exec_start(socket=True)`), a deliberately blocking raw-socket read with no HTTP timeout underneath it (see DEBT-150). Two consecutive faults open the breaker for 60s, refusing further dispatch (not just failing it) so a sustained hang cannot exhaust the pool. `api/runtime.py`'s continuously-polled `_probe_docker`/`_check_image_exists` route through the same helper (`docker_call`) — previously their own separate, unbounded `asyncio.to_thread` calls. A daemon fault degrades to `[sandbox_daemon_unavailable]` and never re-runs `resolve_default_adapter` (ADR-001 held).
- **Files:** `core/sandbox.py` (`_docker_call`, `_DaemonBreaker`, `_get_docker_executor`, `_get_exec_client`), `api/runtime.py`.
- **Verified:** `tests/test_sandbox_pool_resilience.py` (HANG1-7); full suite green; mypy 0; pyright 0.
- **Deferred:** DEBT-150 (the hijacked-PTY-socket case is contained, not eliminated).

---

**MEDIUM**

---

### DEBT-152 [MEDIUM · RESOLVED 2026-08-04, 12.14] — Orphaned agentic-cell PTY sessions can now permanently occupy a bounded pool slot

- **Date:** 2026-08-03 · **Resolved:** 2026-08-04 (12.14)
- **Was:** `sweep_orphaned_sessions(live_task_ids)` closed any registered `_CellSession` whose task was no longer live, but had **zero callers** anywhere in the codebase. Before 12.6, an orphaned cell session (a run aborted mid-loop, Stop button never reaching the node's `finally`) left its PTY session — and the one process-lifetime container it ran on — alive but harmless. After 12.6's bounded per-session pool, that same orphaned session held a lease at `refcount >= 1` forever, immune to `_ContainerPool._reap_expired_idle_locked` (only reaps `refcount == 0`), and could permanently consume the entire pool with the default cap.
- **Resolved:** wired both paths the function's own docstring named. (1) **Run lifecycle (primary):** `TaskService.register_active_task` already attached a `task.add_done_callback` firing on every terminal path (cancel, completion, or fault); a second done-callback now schedules `brain.agentic_cell.close_cell_session(session_id)` fire-and-forget, guarded by two conditions — `has_paused_graph(session_id)` (a native HITL `interrupt()` completes the runner task while `resume_graph` must re-enter the SAME cell session later; closing it here would destroy a live, resumable run) and a successor-task check (the registry holds one task per session; a second runner registering before the first's done-callback fires must not have its cell closed by the predecessor's completion). Fixed an adjacent latent bug the successor guard exposed: the pre-existing pop-callback (`self._active_tasks.pop(session_id, None)`) was not identity-checked, so a predecessor's completion could evict a successor's already-registered entry — now `_pop_active_task_if_current` only pops when the registry still points at that exact task. (2) **WS disconnect (safety net):** a new cleanup hook in `main.py` calls `sweep_orphaned_sessions` itself with `TaskService.live_task_ids()` (the union of `_active_tasks` and `_paused_tasks`, so a paused session is never mistaken for an orphan) — this is the sweep function's real production caller, closing the "wire it" half literally rather than leaving it dead. Fire-and-forget scheduling in both call sites uses the established strong-ref-set-plus-`discard`-done-callback idiom (mirroring `brain/coder_companion.py`), guarded against `asyncio.get_running_loop()` raising when the hook fires outside an active loop (a sync-test-only path in production this never occurs, since disconnect always fires inside the live server's loop).
- **File(s):** `core/task_service.py` (`register_active_task`, `_schedule_cell_teardown`, `_teardown_orphaned_cell`, `_pop_active_task_if_current`, `live_task_ids`), `brain/agentic_cell.py` (`close_cell_session`), `main.py` (`_sweep_orphaned_cells_on_disconnect`).
- **Verified:** `tests/test_agentic_cell_lifecycle.py` (LIFE1-6, incl. the paused-graph and successor guards); full suite green; mypy 0; pyright 0.

---

### DEBT-057 [MEDIUM · RESOLVED 2026-07-25, Phase 11.5] — Non-native-thinking models produce an empty ThoughtBox and appear pre-scripted

- **Date:** 2026-06-14
- **Reproduce:** Select a model that does not support native extended thinking (any non-Anthropic/non-DeepSeek-R1 model). Submit a complex prompt. The ThoughtBox is empty; the agent's response appears as a fixed, pre-scripted answer with no visible reasoning trace, making the AI look unintelligent.
- **File(s):** `ailienant-core/tools/llm_gateway.py` (`_supports_native_thinking`, `acomplete_with_thinking`); `ailienant-core/agents/planner.py` / `agents/coder.py` (system prompt builders); `ailienant-extension/src/webview/components/ThoughtBox.tsx`.
- **Error:** UX/capability gap. Fix: when native thinking is unavailable, inject a reasoning scaffold into the system prompt (`"Before answering, reason step-by-step inside <thinking>…</thinking>"`); stream the `<thinking>` block content via existing `broadcast_thinking_chunk` (identical contract, no new WS type); strip the tags before emitting the final answer token stream. FE: add `[Simulated]` vs `[Native]` tag to ThoughtBox header; add Reasoning Mode toggle (`Native` / `Verbose` / `Compact`).
- **Blocked by:** nothing — fully self-contained.
- **Phase:** Phase 11.5.
- **Notes:** confirmed in live testing session 2026-06-14.
- **Resolution (2026-07-25, Phase 11.5):** shipped as the verbal reasoning fallback + unified reasoning stream; hardened one day later (11.5.1, 2026-07-25) so the reasoning scaffold defaults off for strict-output-contract calls (DEBT-013 recurrence).

### DEBT-058 [MEDIUM · RESOLVED 2026-07-27, Phase 11.6] — Submitted prompt not preserved during task execution; lost in long sessions

- **Date:** 2026-06-14
- **Reproduce:** Submit a long prompt. The input clears immediately. Scroll up in a session with 20+ messages to find the original prompt — difficult to locate. No sticky indicator of what task the AI is currently working on.
- **File(s):** `ailienant-extension/src/webview/components/NattCanvas.tsx`; `ailienant-extension/src/store/workspaceStore.ts`.
- **Error:** UX gap. Fix: Add `activeTaskPrompt: string | null` + `activeTaskId: string | null` to `workspaceStore.ts`; set on submit, clear on `TASK_COMPLETE`/`ERROR`. New `ActiveTaskHeader.tsx` component: sticky card above the message list showing the current task prompt (compressed, expandable), animated "Working…" indicator, elapsed time, Cancel affordance. Auto-collapses on completion; user-dismissible. No backend change — uses existing WS events.
- **Blocked by:** nothing.
- **Phase:** Phase 11.6.
- **Notes:** analogous to Claude Code's in-flight task header. Confirmed need in live testing session 2026-06-14.
- **Resolution (2026-07-27, Phase 11.6):** shipped as the Active Task Header / prompt-preservation feature.

### DEBT-059 [MEDIUM · RESOLVED 2026-07-27, Phase 11.7] — Chat UI has no compaction strategy for long sessions (DOM grows unboundedly)

- **Date:** 2026-06-14
- **Reproduce:** Run a session with 60+ messages. NattCanvas DOM grows unboundedly, causing sluggish rendering and memory pressure in the VS Code webview process. Also mirrors the backend context-window constraint for local models — frequent compaction events would be useful but no FE receiver exists.
- **File(s):** `ailienant-extension/src/webview/components/NattCanvas.tsx`; new `SessionSummaryCard.tsx`; `ailienant-extension/src/store/workspaceStore.ts`; `ailienant-core/api/websocket_manager.py` (new event type); `ailienant-core/brain/context_pipeline.py` (Division 8.12 emission hook).
- **Error:** FE architecture gap + backend integration gap. Fix (two-part): (a) Backend — when `ContextPipeline` (Division 8.12) evicts Layer 4 entries, emit `{"type": "STATE_COMPACTED", "summary": "...", "turns_compressed": N}` over WS. (b) Frontend — when message count exceeds `MESSAGE_COMPACTION_THRESHOLD` (default 40) AND a `STATE_COMPACTED` event arrives, replace messages before the compaction point with a collapsible `SessionSummaryCard` (header: "N messages summarized", body: StateSummarizer output text carried in the event). Messages after the point remain fully rendered.
- **Blocked by:** Division 8.12 `STATE_COMPACTED` event contract (8.12.3).
- **Phase:** Phase 11.7 (FE) + Division 8.12 (backend hook).
- **Notes:** analogous to Claude Code's `/compact` auto-compact. Addresses both DOM memory pressure AND context-window viability for local model sessions. Confirmed need 2026-06-14.
- **Resolution (2026-07-27, Phase 11.7):** shipped as chat compaction for long sessions, closing this entry together with DEBT-078. Residual scope (fold durability across a panel reload) re-logged separately as DEBT-124.

### DEBT-124 [LOW · RESOLVED 2026-08-04, 12.8] — Compaction fold does not survive a panel reload

- **Date:** 2026-07-27 · **Resolved:** 2026-08-04 (12.8)
- **Resolution:** a `CompactionFold` slice (`markerId`, `afterMessageId`, `summaryText`, `turnsCompressed`) persists in `workspaceStore` — the same panel-survivable (hide/reveal, not cross-VS-Code-restart) durability class `inflightTurn` already used for its own display-only resilience. Written when the `state_compacted` chip is created; on render, `Workspace.tsx` falls back to it only when no live chip survives in `messages` (`lastCompactionIdx === -1`), resolving `afterMessageId` against the rehydrated transcript by id — stable across reload, unlike an array index. The fallback boundary row is a real bubble (the anchor message), not a placeholder chip, so its hidden-count arithmetic is `+1` relative to the live-marker case; both paths converge on the same render logic (`i < boundary` hidden, `i === boundary` renders the card only, `i > boundary` normal). "Show original messages" unaffected — no bubble is ever deleted.
- **File(s):** `ailienant-extension/src/workspace/workspaceStore.ts`, `hooks/useWSMessageHandler.ts`, `Workspace.tsx`.
- **Reproduce:** Drive a session past the compaction threshold so the transcript folds behind a `SessionSummaryCard`. Close and reopen the webview panel. The fold is gone — every original message re-renders fully.
- **File(s):** `ailienant-extension/src/workspace/hooks/useSessionPersistence.ts` (system chips stripped from `PERSIST_TRANSCRIPT`); `ailienant-extension/src/workspace/types.ts` (`SystemMessage.compaction`, transient).
- **Error:** FE durability gap (intentional MVP scope). The compaction marker rides on a transient `SystemMessage`, deliberately excluded from the persisted transcript, so the fold boundary + prose are lost on reload while the underlying `ConversationMessage`s persist. This is the safe default (no data loss), but the declutter does not stick. Fix: persist the fold boundary index + `summaryText` (e.g. a dedicated persisted slice keyed by marker id) and re-apply the fold on rehydrate.
- **Blocked by:** nothing — self-contained FE change.
- **Phase:** Phase 11 (deferred from 11.7).
- **Notes:** Non-destructive by design; recoverability via "Show original messages" is unaffected. Deferred to keep 11.7 blast radius contained.

### DEBT-125 [LOW · RESOLVED (display wiring) 2026-08-04, 12.8] — Apply-edge "low-risk" gate is a command-pattern proxy, not an edit-risk classifier

- **Date:** 2026-07-27 · **Resolved (partial):** 2026-08-04 (12.8)
- **Was:** the 11.8 shift-left gate decided "low-risk" by scanning an edit's **added diff lines**
  against `permissions.py::_RISK_PATTERNS` — correct as a fail-toward-the-card heuristic, but the
  FILE_WRITE approval card never received the computed labels (a real display gap, not a classifier
  gap): the backend attached them as `risk_patterns_matched` while the frontend read a dead,
  never-sent `risk_metrics` field.
- **Resolved:** `task_service.py`'s HITL branch now computes a per-file label set (from that file's
  own diff, not the whole batch's auto-accept gate input) and forwards it as
  `risk_patterns_matched` on every FILE_WRITE approval. `HITLInterventionCard.tsx` renders "Flagged
  for: …" whenever the field is present, regardless of `request_kind` (previously gated to
  `RISK_INTERCEPT` only). The dead `risk_metrics` field (`shared/types.ts`, the card's own dead
  render branch) is deleted, not just unused.
- **Deliberately NOT done:** the semantic edit-risk classifier itself — the binary regex proxy is
  unchanged. Re-logged as **DEBT-154** with its own scope, since a real low/medium/high verdict
  changes which edits apply silently and belongs in a dedicated safety slice, not folded into a
  triage sweep.
- **File(s):** `ailienant-core/core/task_service.py`, `ailienant-extension/src/workspace/components/HITLInterventionCard.tsx`, `ailienant-extension/src/shared/types.ts`.

### DEBT-066 [HIGH · RESOLVED 2026-06-20, 8.10.8] — No runtime LLM tool-dispatch loop activates the registered tools

- **Date:** 2026-06-15 · **Resolved:** 2026-06-20 (8.10.8)
- **Reproduce (original):** The orchestrator/coder/analyst tool *classes* register their schemas in the `ToolRAGStore` and (as of 8.10.2) have state/session/search-injecting factories, but no production code path ran an LLM tool-calling loop that constructs and dispatches them. The agents made deterministic `LLMGateway.ainvoke` calls; the registered tools were retrievable but never invoked.
- **Resolved:** built the role-agnostic dispatch substrate `core/tool_dispatch.py` (`ToolCall`, `parse_tool_call_envelope`, `ToolDispatcher`, `make_gateway_reasoner`) generalizing the agentic-cell prompt-enforced-JSON pattern (the gateway returns text, no `bind_tools`). Every dispatch is gated through the same pure `evaluate_action` matrix; the loop is self-correcting (malformed JSON / unknown tool → feedback observation, never a crash). Wired live on the Analyst node (`build_analyst_tools(state)` + a bounded pre-grill loop in `run_analyst_node`), whose six tools are all READ_ONLY → friction-free gate. Executed calls recorded on the additive `tool_dispatch_trace` state channel.
- **Continuation:** the loop is proven on one role only. Extending it to the Coder/Planner/Orchestrator (and adding HITL approval routing for mutating tiers, which the READ_ONLY Analyst path never exercised) is tracked as **DEBT-068**. The Researcher additionally needs node promotion first.
- **Notes:** scope deliberately bounded to substrate + one-node proof so the activation lands with zero mutation blast radius before mutating roles are wired. Supersedes the dispatch half of the former DEBT-043/046/042 and the DEBT-054 channel-wiring concern.

### DEBT-068 [HIGH · RESOLVED 2026-06-21, 8.10.11] — Tool-dispatch loop wired only on the Analyst; mutating-tier HITL routing pending

- **Date:** 2026-06-20 · **Resolved:** 2026-06-21 (8.10.11)
- **Reproduce (original):** `core/tool_dispatch.py` (the 8.10.8 substrate) was invoked only by `run_analyst_node`; `ToolDispatcher.dispatch` returned a "requires human approval" stub for the HITL decision rather than routing to a real approval channel, because the only live consumer was READ_ONLY.
- **Resolved:** `ToolDispatcher.__init__` gains an injectable `approval_fn`; `dispatch` now consults it on a HITL tier (deny-with-report when absent, denied, or raising — never hangs the turn), with a `make_websocket_approval_fn(session_id)` factory wrapping `request_human_approval` + the trust-once valve. The live mutating proof is the coder's existing ReAct loop: `brain/agentic_cell.py::run_terminal` previously treated HITL as ALLOW; a new `_admit_execute` runs the three-axis matrix and routes EXECUTE→HITL through the approval card (PLAN still denies, AUTO still admits). `request_human_approval`'s default deadline raised 300s→86400s.
- **Scope correction (CLAUDE.md §4):** the literal target list did not match the architecture. The **Orchestrator** is a deterministic O(1) node with no LLM/reasoner — a dispatch loop has nothing to drive (permanently excluded). The **Planner** is PLAN-only with READ_ONLY tools — a loop adds no HITL value (excluded). The coder's mutation surface is the agentic cell, not a second `coder.py` loop. The **Researcher** needs node promotion first → carved to **DEBT-069**.
- **File(s):** `core/tool_dispatch.py`, `brain/agentic_cell.py`, `api/websocket_manager.py`; gate `tests/test_phase8_10_11_checkpoint_gate.py`.
- **Notes:** logged at 8.10.8 ship per CLAUDE.md §11.3 as the continuation of DEBT-066.

### ~~DEBT-071~~ [RESOLVED 2026-06-22 · 8.10.15] — LangGraph add_node / langchain args_schema pyright errors

- **Resolution:** 14 `# pyright: ignore[reportArgumentType]` added to `brain/engine.py` `add_node` calls; 47 `# pyright: ignore[reportIncompatibleVariableOverride]` added to `args_schema` overrides across 13 `tools/*.py` files. One pre-existing `reportGeneralTypeIssues` in `mcp_adapter.py` suppressed (Boy Scout). `mypy 0/366`, `pytest 1690 passed`.

### DEBT-073 [LOW · RESOLVED 2026-08-25, 13.1.1] — `plan_mode` string literal appeared 4× in `Workspace.tsx` (DRY)

- **Resolved:** 2026-08-25. Confirmed still reproducing exactly as described (5 occurrences by this point, not 4 — one more had accumulated since the entry was logged). Extracted `PLAN_MODE: ExecutionMode = 'plan_mode'` and `isPlanMode(mode)` in `Workspace.tsx`; every comparison site now calls `isPlanMode(mode)` and the one assignment site (`submitWithMode(trimmed, 'plan_mode')`) now passes the `PLAN_MODE` constant. No behavior change — a pure refactor, so the existing `npm run compile`/`npm run lint`/`npm test` suites are the regression coverage; no new test needed for a same-behavior rename.

- **Date:** 2026-06-23
- **Reproduce:** `grep -n "plan_mode" ailienant-extension/src/workspace/Workspace.tsx` — 4 hits (mode picker guard, HITL routing, plan-doc slot, keyboard shortcut). Each checks the raw string `=== 'plan_mode'`.
- **File(s):** `ailienant-extension/src/workspace/Workspace.tsx` (lines ~1392, ~1672, ~1865, ~1919).
- **Error:** DRY violation. Zero real duplication today because the UI has exactly one `plan_mode` string. Becomes load-bearing debt if a future sub-phase adds `full_auto` or `ask_execute` as a distinct UI button — 4 switch sites must be updated in sync.
- **Blocked by:** nothing — 3-button UI is unchanged and the fix is a 1-line `isPlanMode(mode)` helper. Deferred because 8.11.3 scope explicitly keeps the UI unchanged.
- **Phase:** whichever future sub-phase expands the mode picker beyond 3 buttons.
- **Notes:** logged at 8.11.3 ship per CLAUDE.md §11.3.
### DEBT-072 [MEDIUM · RESOLVED 2026-06-24, 8.10.16] — Pending-interrupt restart-durability

- **Date:** 2026-06-22 · **Resolved:** 2026-06-24 (8.10.16)
- **Reproduce (original):** a native HITL interrupt (8.10.14) pauses the graph in L1 (`MemorySaver`) and frees the runtime; the pause survives within a server lifetime. But `HybridCheckpointer.promote()` persisted the checkpoint + pending writes to L2 while `recover()` restored only the checkpoint values — not `hybrid_writes_l2` — so a server restart mid-interrupt lost the pending-interrupt task marker and orphaned the resume.
- **Fix:** `recover()` now re-seeds the pending writes (incl. the paused `interrupt()`) via `put_writes` (`_restore_pending_writes`); `promoted_at` switched `time.monotonic()`→`time.time()` (+ `checkpoint_id` tie-break) so cross-restart ordering can't resurrect a stale interrupt after a resume re-promotes the cleared head; `write_idx` enumerated to stop multi-write PK collisions; `arecover`/`apromote` async offload wrappers keep the FastAPI/WS event loop unblocked; `task_service.rehydrate_paused_interrupt` (wired at `client_restore_history`) re-arms `_paused_tasks` and re-emits the approval card on reopen, and the resume branch seeds `session_permission_mode` from the recovered checkpoint so the out-of-graph MCP gate honors the saved posture.
- **File(s):** `brain/checkpoint.py`, `core/task_service.py`, `main.py`; gate `tests/test_phase8_10_16_checkpoint_gate.py` (5 rows).
- **Notes:** carved at 8.10.14 ship per CLAUDE.md §11.3. Exact original `TaskPayload`/thinking-config fidelity across a restart is the declared MVP boundary → DEBT-079.

### DEBT-079 [LOW · RESOLVED 2026-08-03, 12.5] — Cross-restart HITL resume reconstructs a minimal TaskPayload

- **Date:** 2026-06-24 · **Resolved:** 2026-08-03 (12.5)
- **Reproduce (original):** after a server restart, `rehydrate_paused_interrupt` re-arms the paused task with a minimal `TaskPayload(task_prompt="", dirty_buffers=[])`; the orchestration mode and security posture are recovered from the checkpoint, but the original prompt/attachments and thinking-config (`enable_native_thinking`, `thinking_budget_tokens`) fall back to defaults for any *new* reasoning turns produced after the resume.
- **Reclassified from fidelity to correctness during 12.5 investigation:** the original entry's premise — "not a correctness gap" — did not hold. `payload.task_prompt` is still read on the shared post-resume path (`_run_coding_task`'s `_append_history(session_id, "user", payload.task_prompt)` calls, unguarded by `resume_value`), so the pre-fix empty string wrote a **blank user message into the persisted transcript** on every restart-resume.
- **Resolved:** two additive `AIlienantGraphState` channels (`enable_native_thinking`, `thinking_budget_tokens`) mirror the reasoning-mode config that previously lived only in `config["configurable"]` and was never checkpointed; seeded once at task start, they ride `HybridCheckpointer`'s existing promotion for free. `rehydrate_paused_interrupt` now also reads the original `user_input` (already a state channel, following the `execution_mode` precedent) straight from `snapshot.values`. `dirty_buffers`/`attachments` remain deliberately unpersisted — the §6.3 secrets-hygiene risk this entry originally declined stands.
- **File(s):** `brain/state.py`, `core/task_service.py`; `SCHEMA_EVOLUTION.MD §44`.
- **Notes:** an architecture review during 12.5 flagged mixing immutable task config with mutable runtime state on principle; accepted here because `HybridCheckpointer` promotes once per run (not per step, "zero IOPS" L1), and `execution_mode` already sets this precedent. See DEBT-145 for the logged separation, triggered only once a third piece of per-task config needs this.

### DEBT-080 [MEDIUM · RESOLVED 2026-07-01, 8.14.0] — Dependency-graph edge extraction is Python-only

- **Date:** 2026-06-24
- **Reproduce:** index a TypeScript/Go/Rust file — it lands in `indexed_files` with a symbol count and FTS-grep coverage, but `index_file_sync` (`brain/memory.py`) extracts import edges only under `if tree is not None and req.language_id == "python"`, so `dependency_graph` gains **zero edges** for it. GraphRAG's relational layer (`_bfs_k_hop`, PPR centrality) and every graph-reading capability — 8.14.1 blast-radius, 8.14.3 dead-code — are therefore silently Python-only.
- **Error:** architectural coverage gap, not a defect. Detection (`_EXT_LANG`, 21 langs), AST parsing (tree-sitter, polyglot), and symbol/grep indexing are already language-agnostic; only dependency-edge extraction is language-bound to Python.
- **Resolution (scheduled, 8.14.0):** a `language_id`-dispatched `IMPORT_EXTRACTORS` registry (Python refactor + TS/JS reference); relative-specifier resolution is **lexical and disk-free** in the extractor (the per-file `ProcessPool` worker has no view of the file tree), with extension/`index.*` candidate expansion done in `_resolve_edge_confidence` against the `indexed` set (no phantom edges, no `dependency_graph` schema change); a workspace-boundary guard drops cross-root edges via an additive `IndexingRequest.workspace_root`. Further languages are additive registry entries, demand-gated (Phase 12 corpus), not speculative.
- **Distinct from DEBT-075:** 080 is about graph **edges** (dependency topology); 075 is about **symbol typing** (LSP-style type resolution). Independent.
- **Phase:** 8.14.0.
- **Notes:** logged at 8.14 planning per CLAUDE.md §11.3; resolves the latent Python-only assumption under Division 8.14's "substrate already exists" premise.
- **Resolution (2026-07-01, 8.14.0):** shipped the `IMPORT_EXTRACTORS` registry (Python + TS/JS) with lexical disk-free relative resolution, a strict workspace-boundary guard (`IndexingRequest.workspace_root`), and extension/`index.*` candidate expansion in `_resolve_edge_confidence`. The dependency graph is now polyglot. TS/JS relative resolution surfaced a Python asymmetry, logged as DEBT-087.

### DEBT-094 [LOW · RESOLVED 2026-07-02] — 38 pre-existing project-wide pyright errors

- **Date:** 2026-07-02
- **Reproduce:** `cd ailienant-core && npx pyright` → 38 errors across 13 files, none touched by 8.14.7; confirmed present on a clean `HEAD` tree (stash test). Invisible to the enforced `mypy .` gate (0/392).
- **Error:** three clusters — (A) ~19 third-party stub gaps (docker `.errors`, lancedb `LanceQueryBuilder.metric` / `table_names` union, `pyarrow.compute.equal`, pynvml `.total`, langgraph `HybridCheckpointer.wal_checkpoint`); (B) 10 in `tools/llm_gateway.py` from litellm's `ModelResponse | CustomStreamWrapper` union (+ one benign possibly-unbound); (C) 9 LangGraph typed-state / test-double mismatches.
- **Resolution:** dedicated cleanup commit (separate from 8.14.7) — narrowed the litellm union on the `stream` branch, defensive-init `_effective_timeout`, and justified `# type: ignore`/`cast` for the stub-gap false-positives.
- **Notes:** resolved in a dedicated follow-up commit; `npx pyright` → 0 errors, `mypy .` unchanged at 0/392.

### DEBT-104 [LOW · Floating] — Tournament surface rollback does not delete candidate-introduced files

- **Date:** 2026-07-03
- **Reproduce:** `run_tournament` (`brain/subagent_tournament.py`) rolls the shared surface back to the clean base between candidates by calling `push_vfs_to_surface(surface, base_vfs, ...)`. `push_vfs_to_surface` (`core/workspace_sync.py`) only *writes* the paths in the pushed set — it never removes surface files absent from it. So a candidate that introduces a new path (one not in `clean_base_content`) leaves that file on the surface after the rollback; it then contaminates the next candidate's verify run and the final winner restore.
- **Error:** harmless for the agentic cell, whose competing candidates are edits to the *same existing* file set (no new paths leak). Exposed only by `run_tournament_from_dispatch`, where independent subagents may each propose different new files. The relocated body is byte-identical (R2), so the fix belongs at the caller/isolation layer, not in the relocated engine. Today the adapter emits a `logger.warning` (naming the out-of-base paths) but still evaluates the candidate — new-file candidates are legitimate for `generate`.
- **Resolution (owned by 8.15.5 wiring):** give the tournament a delete-not-in-base rollback path (or snapshot/restore the full surface path set) so heterogeneous dispatch candidates are fully isolated, then drop the advisory warning.
- **Notes:** logged at 8.15.3 close; the warning uses the `SUBAGENT_TOURNAMENT` logger and is asserted in `tests/test_subagent_tournament.py::test_from_dispatch_warns_on_out_of_base_path`.


### DEBT-105 [LOW · Floating] — Dispatch cost accounting is estimate-based and under-counts

- **Date:** 2026-07-03
- **Reproduce:** `brain/dispatch_ledger.py::estimate_task_cost` calls `estimate_iteration_cost([{seed}], [])` × `max_iterations`. With no known outputs it under-models completion tokens (typically 3–5× input) and the per-iteration context growth, so a reserved wave estimate is lower than true spend → `reserve_dispatch_budget` admits more than a precise model would. Separately, `subagent_worker`'s `cost_usd = estimate_iteration_cost(loop_messages, trace)` meters only the tool loop, not the `answer_fn` synthesis LLM call; and `commit_dispatch_actual` is refund-only, so an `actual > reserved` overage is never re-charged.
- **Error:** deliberate scope for 8.15.4 — the primitives are exact (deny/refund/floor tests pass); the inaccuracy is in the *estimate*, which is a soft admission gate, not the hard ceiling. `brain/finops.py` + `brain/iteration_governor.py::check_governor` still enforce the real `max_budget_usd`/token/time ceilings, so a lenient dispatch estimate cannot actually overspend the task budget — it only makes admission permissive.
- **Resolution (owned by 8.15.5 wiring):** when the dispatch nodes are wired, meter real gateway token usage (the `answer_fn`/`LLMGateway` seam) for both the reserve estimate and the committed actual, and decide overage handling (book it as a signed commit delta, or keep refund-only with a tighter upper-bound estimate).
- **Notes:** logged at 8.15.4 close; the node/edge admission wiring that consumes these primitives is 8.15.5.


### DEBT-110 [LOW · RESOLVED 2026-07-24, 11.4] — BYOM panel styles reference unresolved host/legacy CSS variables

- **Date:** 2026-07-20 · **Resolved:** 2026-07-24 (11.4)
- **Was:** the `.byom-*` rules in `ailienant-extension/src/dashboard/dashboard.css` referenced `--vscode-editor-background`, `--vscode-sideBar-background`, `--vscode-panel-border`, `--vscode-descriptionForeground` (host webview variables, absent in the browser-served SPA) and `--color-primary`/`--color-card`/`--color-text`/`--color-border`/`--color-primary-soft` (never defined anywhere). Declarations without an inline hex fallback (e.g. `.byom-confirm-modal { background: var(--vscode-editor-background) }`) rendered transparent/broken; 11.0 added only a transitional compatibility shim in `shared/theme.css`.
- **Resolution:** commit `6ae8eb3` ("feat(11.4): BYOM & Extensions dashboard polish + per-model cost badges") rewrote the BYOM panel onto the 11.0 design system as part of its full redesign — `dashboard.css`'s `.byom-*` rules now reference real design tokens (`--accent-primary`, `--border-subtle`, `--status-good`, `--space-3`, …) directly, with zero remaining `--vscode-*` or bare `--color-*` references anywhere in the file (grep-verified). The BYOM-local confirm-modal rule was replaced entirely by the shared `ui/ConfirmModal` component's own styling (see DEBT-112) — never marked closed in this ledger until this pass, discovered by reading the current code rather than trusting the stale row.
- **Files:** `ailienant-extension/src/dashboard/dashboard.css`, `ailienant-extension/src/dashboard/panels/BYOMPanel.tsx`.

### DEBT-112 [LOW · RESOLVED 2026-07-24, 11.4] — `BYOMPanel.tsx` used its own `byom-confirm-*` markup instead of the shared `ui/ConfirmModal`

- **Date:** 2026-07-20 · **Resolved:** 2026-07-24 (11.4)
- **Was:** `ui/ConfirmModal` was extracted as a shared primitive from BYOM's own inline confirm modal, but `BYOMPanel.tsx` kept using its original `byom-confirm-*` markup/CSS instead of migrating onto it — a DRY gap that never got its own body entry in this ledger (dashboard-row-only until this pass).
- **Resolution:** the same 11.4 redesign (commit `6ae8eb3`) migrated `BYOMPanel.tsx` onto the shared component — it now imports `ConfirmModal` from the `ui` barrel and renders it directly (no local `byom-confirm-*` modal markup remains); `dashboard.css`'s confirm-modal styling is now the shared `/* Generic confirm modal (ui/ConfirmModal) */` block, not a BYOM-local one.
- **Files:** `ailienant-extension/src/dashboard/panels/BYOMPanel.tsx`, `ailienant-extension/src/dashboard/ui/ConfirmModal.tsx`, `ailienant-extension/src/dashboard/dashboard.css`.
- **Notes:** logged retroactively — a case for the ledger itself (cf. DEBT-027's own "stale note outlived the fix" precedent): the fix landed with 11.4 but the tech-debt entry was never written or closed, so it sat as an open dashboard row with zero detail for weeks.

### DEBT-077 [MEDIUM · RESOLVED 2026-06-26, 8.10.17] — Unify analyst ContextBudgetManager onto ContextPipeline

- **Resolved:** 2026-06-26 (8.10.17)
- **Reproduce (original):** the analyst ran its own `ContextBudgetManager` tier-ladder packer (priority-drop with per-brain 60% soft-caps) in `agents/analyst_context.py`, parallel to the canonical five-layer `ContextPipeline` planner/coder use; ladder brain keys did not map to pipeline layer labels.
- **Fix:** `assemble_analyst_context` now routes its sources through `build_agent_context` — CODEX→Foundation, README+GraphRAG→Project, docs+active-file→Execution — with the per-tier budget passed as `total_token_budget`. `ContextBudgetManager` and the ladder/soft-cap constants were deleted; a `ContextBudgetError` path drops the Project layer wholesale on overflow; a G3 repair guard re-appends the file block's closing boundary tag when Execution-layer truncation cuts it; a `_G3_OVERHEAD_TOKENS` reserve keeps the post-assembly raw-data clause + repair tag within the tier budget.
- **File(s):** `agents/analyst_context.py`; tests migrated in `tests/test_analyst_brains.py`; `brain/context_pipeline.py` (docstring), `docs/SYSTEM_PROMPTS.md`.
- **Notes:** the pipeline has no soft-cap layer, so anti-starvation became "pinned L1-L3 + degrade". The behavioral residue (budget under-fill, coarse degrade) is tracked as DEBT-081.

### DEBT-081 [MEDIUM · Floating] — Analyst context under-fills the tier budget on the shared pipeline

- **Date:** 2026-06-26
- **Reproduce:** `assemble_analyst_context(tier="medium")` with a large active file returns a block using only `foundation + project + ~1/3 of the post-foundation remainder`. `ContextPipeline.assemble` (`brain/context_pipeline.py`) splits the post-anchor remainder L4=2/3 / L5=1/3 with no reallocation when L4 is empty — and the single-shot analyst has no Conversation (L4) layer, so ~2/3 of its budget is unusable and file+docs (L5) are squeezed into the third.
- **Error:** efficiency + prioritization regression vs the retired packer, not a correctness defect — output is always ≤ budget and never crashes. Facets: (a) empty-L4 under-fill; (b) the Project-layer `ContextBudgetError` degrade drops README+GraphRAG wholesale where the old per-brain 60% soft-cap shed them gradually; (c) L5 truncates file+docs at one uniform ratio so the active file cannot be prioritized over docs.
- **Blocked by:** nothing; deferred to keep DEBT-077 a contained consolidation.
- **Phase:** future context slice. Candidate fixes: reallocate an empty layer's budget to its siblings in `ContextPipeline`, or analyst-tuned layer fractions.
- **Notes:** carved at 8.10.17 ship per CLAUDE.md §11.3.
- **Decision (2026-08-04, 12.14 — CLAUDE.md §4 Option A, Pivot):** explicit defer, reviewed rather than
  silently carried forward. This is a quality nuance (efficiency + prioritization), not a correctness
  break — output is always ≤ budget and never crashes. A point patch to the empty-L4 tier split would
  trade one under-fill pattern for another; the real fix needs its own budget-reallocation design across
  `ContextPipeline`'s layer fractions, which is out of scope for a pre-launch debt-closure pass. Not
  required for Phase 13.
- **Facet (a) RESOLVED (2026-09-01):** `brain/context_pipeline.py::_split_discretionary` replaces the
  static L4=2/3 / L5=1/3 split with a demand-aware one — each layer is offered its declared share and a
  layer wanting less releases the difference to the other. Shares derive from the layers' own
  `budget_fraction` (0.30/0.45), so the ratio is no longer restated. With both layers over-share the
  allocation is byte-identical to the old split, so nothing regresses when the conversation is real.
  Separately, `agents/analyst_context.py` now sizes its budget from the serving model's probed window
  (`resolve_real_window`) bounded by `AILIENANT_ANALYST_BUDGET_FRAC`/`_CEILING`, with the former static
  tier table kept only as a floor — the analyst was budgeting 3000 tokens against a 1M-token window.
- **Still open — facets (b) and (c):** the Project-layer `ContextBudgetError` degrade still drops
  README+GraphRAG wholesale rather than shedding them gradually, and L5 still truncates file+docs at one
  uniform ratio with no way to prioritize the active file over docs. Tier stays MEDIUM for those.


### DEBT-070 [HIGH · RESOLVED 2026-06-22, 8.10.14] — Async-sleep HITL waits block a coroutine until timeout/response

- **Date:** 2026-06-21 · **Resolved:** 2026-06-22 (8.10.14)
- **Reproduce (original):** every in-graph HITL gate suspended the calling coroutine on an `asyncio.Event`/`wait_for` until the human responded or a wall-clock deadline fired, pinning the graph super-step for the duration.
- **Resolved:** added a native Suspend & Resume substrate (`core/hitl.py::request_graph_approval` → LangGraph `interrupt()`; `extract_pending_interrupt` via `aget_state`). `task_service` detects the pause post-`astream` (the generator ends naturally; never via `except`), emits the approval card, and frees the runtime; `resume_graph` re-enters the checkpointed thread with `Command(resume=…)`. Converted: FinOps (single node — gate on committed state), DriftMonitor (split `drift_compute`→`drift_gate` so the interrupt-bearing node decides on already-committed, replay-stable state), and the agentic cell (defer the gated command → interrupt-first exec-approval phase, so no side effect is replayed and the command runs once). The dormant `tool_dispatch.make_websocket_approval_fn` seam was re-pointed for the first future consumer.
- **File(s):** `core/hitl.py` (new), `brain/finops.py`, `brain/drift_monitor.py`, `brain/agentic_cell.py`, `brain/engine.py`, `brain/state.py`, `core/task_service.py`, `core/tool_dispatch.py`, `main.py`.
- **Notes:** non-graph HITL (MCP adapter, post-graph file-write apply loop) intentionally stays on the `request_human_approval` event path — `interrupt()` only works inside a running graph node. Restart-durability of a pending interrupt is carved as DEBT-072.

### DEBT-069 [MEDIUM · RESOLVED 2026-06-21, 8.10.12] — Researcher is not a graph node; needs promotion before it can host a dispatch loop

- **Date:** 2026-06-21 · **Resolved:** 2026-06-21 (8.10.12)
- **Reproduce (original):** `agents/researcher.py` was a deterministic single-shot retrieval + one `LLMGateway.ainvoke`; its skeleton was consumed only as optional Planner context and it was not registered as a node in `brain/engine.py`, so it could not host a `ToolDispatcher` loop.
- **Resolved:** promoted to a first-class node (`researcher_agent`, spliced before `planner_agent` via the dict path-map remap) with a bounded READ_ONLY `ToolDispatcher` grounding loop (`tools/researcher_tools.build_researcher_tools`). Scope was expanded (user-directed) to full SRP consolidation: all retrieval + the Context Meter Cascade + hardware reroute were relocated from the Planner to the Researcher, which now emits the routing signal (`context_metrics`/`css`/`tci`/`provider`/`routing_warning`) + a dense AST skeleton; the Planner became a pure WBS engine. SCHEMA_EVOLUTION.MD §19 documents the producer move.
- **File(s):** `agents/researcher.py`, `agents/planner.py`, `tools/researcher_tools.py`, `brain/engine.py`; gate `tests/test_phase8_10_12_checkpoint_gate.py` + ~17 migrated routing/cascade/fast-boot tests.
- **Notes:** the routing-spine math was relocated verbatim (same thresholds/order) so behavior is identical; carved from DEBT-068 at 8.10.11.

### DEBT-067 [LOW · RESOLVED 2026-08-03, 8.10.3] — Hardware stress simulator uses synthetic injection, not real allocation

- **Date:** 2026-06-19 · **Resolved:** 2026-08-03 (12.5)
- **Reproduce (original):** `tests/chaos/test_hardware_stress_sim.py` applies memory pressure by injecting a starved `HardwareProfile` (monkeypatching `HardwareDetector.detect`) rather than actually allocating RAM/VRAM. The graceful-degradation reroute and its telemetry row are validated deterministically, but the detector's real probing path (pynvml / psutil under genuine pressure) is not exercised.
- **Resolved:** the synthetic contract test is untouched (still the correct CI-safe check); a new opt-in `scripts/hardware_stress_sim.py` (`AILIENANT_ENABLE_HW_STRESS=1`, never pytest-collected) allocates real RAM in bounded chunks toward a `--target-free-gb` floor (clamped at a hard `_MIN_SAFE_FREE_GB`, with a stall detector against a misreporting host) and calls the real `HardwareDetector.detect()` under that pressure. VRAM pressure (`--vram`) is honest rather than faked: probing a GPU (`pynvml`) is not the same as consuming its memory, which needs a compute framework (torch/cupy) this project deliberately does not depend on (Charter §9 precedent — `scipy` rejected likewise); when no such framework is importable, VRAM stress is explicitly skipped with a printed reason, never silently omitted.
- **File(s):** `ailienant-core/scripts/hardware_stress_sim.py` (new); `tests/chaos/test_hardware_stress_sim.py` unchanged.
- **Verified:** run manually on a Windows dev host with no discrete GPU — real RAM dropped from 2.9 GB to 2.54 GB available and was fully released afterward; `suggested_mode` correctly reported it could not degrade further via RAM pressure alone on that platform (`effective_vram_gb` gates on GPU VRAM, not system RAM, for every non-Apple-Silicon host) rather than printing a misleading "try a lower target".
- **Notes:** logged at 8.10.3 ship per CLAUDE.md §11.3; the user chose synthetic injection for the CI-safe contract test at that time, which stands unchanged.


### DEBT-044 [MEDIUM · RESOLVED 2026-06-20, 8.10.10] — ValidateWBSDependenciesTool detects forward-reference ordering violations only, not true DAG cycles

- **Date:** 2026-06-14 · **Resolved:** 2026-06-20 (8.10.10)
- **Resolved:** `WBSStep.depends_on: Optional[List[int]] = None` added additively to `brain/state.py` (backward-compatible; existing checkpoints deserialize as `None`). `ValidateWBSDependenciesTool._arun()` gains Pass 5 — Kahn's BFS topological sort over `depends_on` links; a cycle or invalid reference becomes a blocking issue (`"dependency_cycle"` / `"invalid_depends_on"`, both setting `valid = False`). `SCHEMA_EVOLUTION.MD §18` documents the new field and its contract. Note: the original debt spec referenced §15 — corrected to §18 (§15 was already taken by External Gateway Catalog).
- **Notes:** Pass 5 is a no-op when no step declares `depends_on`, preserving all existing test behavior.

### DEBT-047 [LOW · RESOLVED 2026-08-03, 12.5] — generate_docstring is line-anchored, not a signature-aware renderer

- **Date:** 2026-06-14 · **Resolved:** 2026-08-03 (12.5)
- **Reproduce (original):** Call `generate_docstring` on a multi-line `def`/`class`. It inserts a `"""TODO: document <name>."""` stub as the first body statement; it does not synthesize param/return sections from the signature, and it deliberately SKIPs single-line definitions (`def f(): return 1`).
- **Resolved:** `DocstringGeneratorTool._arun` now synthesizes real Google- or Numpy-style sections (new `style` input field, default `"google"`) from the already-parsed AST — Args/Parameters (with type + default, `self`/`cls` dropped on non-static methods), Returns (omitted for no annotation or an explicit `-> None`), Raises (direct-scope `ast.Raise` nodes only, deduped and capped), and Attributes for a `ClassDef`'s class-level annotations. The single-line gap is closed: the header is split from its body via `ast.unparse`-regenerated statements (not a substring slice, which cannot safely relocate a semicolon-joined multi-statement body) rather than the previous unconditional SKIP. `_validate_python_syntax` remains the safety gate for both paths.
- **File(s):** `ailienant-core/tools/coder_tools.py` (`GenerateDocstringInput`, `DocstringGeneratorTool`, new render helpers); `tests/test_phase8_8_5_coder_arsenal.py` (rewrote the now-inverted single-line SKIP assertion into a render assertion; added Google/Numpy/method/class-attribute rows).
- **Notes:** logged at 8.8.5 ship per CLAUDE.md §11.3; the AST-anchored insertion + syntax-validation safety net from that ship is unchanged.

### DEBT-048 [MEDIUM · RESOLVED 2026-06-20, 8.10.6] — RunBenchmarkTool skips task_service.register_active_task

- **Resolved:** added a module-level `get_task_service()` accessor (the DI seam the blocker named) + `reset_task_service()` for test isolation; `RunBenchmarkTool._arun` now `register_active_task(task_id, runner)` (benchmark uuid is a distinct key namespace from UI session ids → no abort-mesh clobber), mirroring the host submit endpoint. `check_task_status`/`get_task_status` now report the run as running.
- **Date:** 2026-06-14
- **Reproduce:** Submit a benchmark via `run_benchmark` tool and then poll `check_task_status` with the returned task_id. `check_task_status` routes through `task_service.get_task_status()` which only knows about tasks registered via `register_active_task`. Since the tool bypasses that call, it returns `{"status": "unknown"}`. `get_benchmark_report` still works (it reads the artifact file directly).
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`RunBenchmarkTool._arun`).
- **Error:** declared trade-off (CLAUDE.md §11.2). `task_service` is a singleton wired at lifespan startup — tools have no dependency-injection path to it currently.
- **Blocked by:** a shared benchmark/task lifecycle service that exposes `register_active_task` via a module-level accessor or injection point.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-049 [LOW · INVALID 2026-08-01, 12.3] — SkillInvokeTool embed_fn=None disables semantic auto-matching

- **Invalid:** the premise was false. `core/skill_resolver.py::resolve_active_skills` reads `embed = embed_fn or _resolve_default_embed_fn()` — `embed_fn=None` selects the resolver's own default embedder, it disables nothing. `git log -S` shows that fallback has existed since the file's one and only commit (`92d0537`), so the code never behaved as this entry described; production (`core/task_service.py`) already relies on the exact same fallback for every task. No embedder-factory fix was needed or built. The one real, load-bearing finding this investigation surfaced: `skill_invoke` is architecturally unreachable via `core/tool_registry.py::resolve_tools()` regardless of its embedder — its only runtime consumer (the agentic cell) always runs under a WBS coder role, and `skill_invoke`'s `allowed_roles` (`{orchestrator, planner}`) is disjoint from that set, with neither role running a dispatch loop (DEBT-068). Adding a factory would have been unreachable dead code, so none was added — only the tool's docstring/description (previously self-describing as "disabled") and the registry's exclusion rationale (previously the wrong "gateway duplicate" reason — see DEBT-131) were corrected.
- **Date:** 2026-06-14
- **Reproduce (original, no longer valid):** Call `skill_invoke` with only `user_input` (no `skill_id`). ~~Without an embedder, `resolve_active_skills` falls back to the explicit-ID path and returns an empty list~~ — this claim does not hold; auto-matching runs.
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`SkillInvokeTool`), `ailienant-core/core/tool_registry.py` (`_INTENTIONALLY_UNREGISTERED`), `ailienant-core/tests/test_phase12_3_integration_debts.py` (SKILL1-5 regression locks).
- **Error:** originally recorded as a declared trade-off; on investigation, not a trade-off at all — a stale/incorrect description of the code.
- **Phase:** 12.3.
- **Notes:** see DEBT-131 for the corrected `_INTENTIONALLY_UNREGISTERED` rationale shared with `list_capabilities`/`task_list`/`task_stop`.

### DEBT-050 [MEDIUM · RESOLVED 2026-06-20, 8.10.6] — RunBenchmarkTool does not charge ledger.consume_budget

- **Resolved:** `RunBenchmarkTool._arun` charges `ledger.consume_budget("internal:agent", cost)` upfront before dispatch (cost via a local `_benchmark_cost()` reading the same env var as the gateway handler), with refund-on-failure and slot-release compensation on every edge. Charge only — budget-ceiling *enforcement* stays the gateway handler's job.
- **Date:** 2026-06-14
- **Reproduce:** Internal agents invoking `run_benchmark` tool bypass the `ledger.consume_budget()` call that the external gateway handler applies. Benchmark compute cost is unaccounted for in the token ledger.
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`RunBenchmarkTool._arun`); compare `gateway/handlers.py` (`handle_run_benchmark`).
- **Error:** declared trade-off. A cross-cutting budget interceptor (pre-action hook on EXECUTE-tier tools) would close this uniformly.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-051 [LOW · RESOLVED 2026-06-20, 8.10.10] — task_list cross-role visibility (orchestrator sees all tasks)

- **Date:** 2026-06-14 · **Resolved:** 2026-06-20 (8.10.10)
- **Resolved:** `BackgroundTaskManager.create()` now accepts `owner_role: Optional[str] = None` and stamps it into the task registry entry. `list_tasks(caller_role)` filters the snapshot so non-orchestrator callers see only their own tasks; `caller_role="orchestrator"` or `None` returns the full view (backward-compatible default). `TaskCreateInput` gains `owner_role` field; `TaskCreateTool._arun()` threads it to the manager. `TaskListInput` gains `caller_role` field; `TaskListTool._arun()` passes it to `list_tasks()`. Changes are additive — callers that don't supply the new fields get unchanged behavior.
- **Notes:** accelerated from Phase 12.3 to close before 8.11 inherits the gap.

### DEBT-052 [LOW · INVALID 2026-08-03, 12.5] — resolve_active_skills may execute synchronous LanceDB queries

- **Invalid:** the premise was false, the same failure mode 12.3 found in DEBT-049. `core/db.py::get_skill` and `list_enabled_skills_for_scope` are **aiosqlite** — genuinely async, nothing blocks the event loop. There was never a LanceDB call on this path. Two real, unrelated defects sat next to the phantom one and are what 12.5 actually fixed: (1) `resolve_active_skills` re-embedded every enabled skill's description on **every task**, serially, for text that essentially never changes turn-to-turn — a fresh network round-trip per skill per turn; fixed with a bounded LRU (`core/skill_resolver.py::_DescriptionEmbedCache`, content-keyed so an edited description simply becomes a new key — no TTL needed for correctness) plus concurrent (not serial) misses. The query vector is deliberately never cached (unique per task; caching it would be an unbounded leak). (2) `_resolve_default_embed_fn` imported `core.tool_rag` (pulling in `lancedb`/`pyarrow`) lazily inside a sync call on the loop; moved off-loop via `asyncio.to_thread`. An in-flight review draft proposed an unbounded module-global cache — rejected: this is a pure function cache (description text → vector) where a multi-worker miss just recomputes the same value (never an inconsistency), so bounding for memory is what actually mattered, not a TTL or worker-affinity scheme.
- **Date:** 2026-06-14
- **Reproduce (original, no longer valid):** Call `skill_invoke` with a valid skill_id. ~~`resolve_active_skills` is `async def` but internally calls `catalog_db.get_skill()` / `list_enabled_skills_for_scope()` which may be synchronous LanceDB queries~~ — this claim does not hold; both are aiosqlite.
- **File(s):** `ailienant-core/core/skill_resolver.py` (`_DescriptionEmbedCache`, `resolve_active_skills`); `ailienant-core/tests/conftest.py` (autouse cache reset — the two 12.3 regression tests both seed a skill described "candidate skill", which would otherwise leak a cache hit between them); new `tests/test_phase12_5_quality_sweep.py`.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3; corrected at 12.5.

### DEBT-053 [LOW · RESOLVED 2026-06-20, 8.10.6] — TaskStopTool uses SIGTERM only, no SIGKILL escalation

- **Resolved:** `BackgroundTaskManager.stop` is now async — commits `cancelled`, sends the soft signal, polls `returncode` to a 5 s grace deadline (no double-await against `_watch`'s `communicate()`), then escalates: POSIX `proc.kill()` (SIGKILL), Windows `taskkill /PID … /T /F` (tree, via the non-blocking asyncio subprocess). Sole caller `TaskStopTool._arun` was already async.
- **Date:** 2026-06-14
- **Reproduce:** Use `task_stop` on a process that traps SIGTERM. The process ignores the signal and keeps running; `_registry["status"]` is "cancelled" but the PID is still alive.
- **File(s):** `ailienant-core/tools/execution_tools.py` (`BackgroundTaskManager.stop`).
- **Error:** declared trade-off. A kill-after-timeout pattern (SIGTERM → wait N seconds → SIGKILL / TerminateProcess) is the correct fix.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-054 [LOW · RESOLVED 2026-08-01, 12.3] — `todo_write` / `agent_todos` channel have no runtime call site

- **Resolved:** half of this was already closed by Division 8.18 (`core/tool_registry.py` wired `todo_write` into the agentic cell's fallback dispatch — `test_agentic_cell_tool_registry.py` proves it) before this entry was updated to say so. The remaining gap: the tool's `{"agent_todos": [...]}` payload only ever became a trajectory observation string, never reached the state channel or the UI. Closed via an allowlisted state-promotion seam (`core/tool_dispatch.py::promote_tool_state`, `DispatchResult.state_delta`, `_STATE_PROMOTERS = {"todo_write": "agent_todos"}`) folded into `brain/agentic_cell.py`'s returned delta (both the normal and the HITL-deferred `defer_delta` early-return path — a write preceding a deferred `run_terminal` in the same iteration must not be lost), a new additive `server_agent_todos` WS event (`api/ws_contracts.py` §42, `api/websocket_manager.py`), and a frontend `AgentTodoPanel.tsx` reading `workspaceStore`'s `agentTodos` slice. Two review-identified risks hardened: (1) an event-loop parse-ceiling — `promote_tool_state` checks `len(raw) > MAX_JSON_PARSE_CHARS` (50,000, `shared/config.py`) BEFORE any `json.loads`, since the observation is read pre-truncation (truncating first would corrupt the JSON) and `TodoItem`'s string fields had no upper bound; (2) emit idempotence — the WS emit fires at most once per cell iteration (after the tool-call loop, not inside it) and is suppressed when the promoted list is value-equal to the channel's prior committed value, and the frontend setter independently keeps the same array reference on a deep-equal write so a redundant event (if one still arrives) causes no re-render. Incidentally found and fixed a latent bug this closure exposed: `ToolDispatcher.dispatch` calls `reg.tool._arun(**call.args)` directly, bypassing LangChain's `args_schema` validation, so `todos` arrived as raw dicts rather than `TodoItem` instances — any real (non-empty) `todo_write` call through this path would `AttributeError` on `item.status`; every prior passing test only ever used an empty list. `TodoWriteTool._arun` now coerces each raw item via `TodoItem.model_validate`, dropping (not failing on) a single malformed item.
- **Date:** 2026-06-14
- **Reproduce (original, no longer valid):** `TodoWriteTool._arun` returns a well-formed JSON string keyed `agent_todos`; `brain/state.py` has the channel and reducer. ~~However, no graph node calls this tool~~ — Division 8.18 already made that untrue; the remaining gap was promotion into the channel, not reachability.
- **File(s):** `ailienant-core/tools/universal_tools.py`, `ailienant-core/brain/state.py`, `ailienant-core/core/tool_dispatch.py`, `ailienant-core/brain/agentic_cell.py`, `ailienant-core/brain/cell_dispatcher.py`, `ailienant-core/api/ws_contracts.py`, `ailienant-core/api/websocket_manager.py`, `ailienant-core/shared/config.py`; `ailienant-extension/src/api/contracts.ts`, `.../workspace/workspaceStore.ts`, `.../workspace/hooks/useWSMessageHandler.ts`, `.../workspace/utils/messageDispatchHelpers.ts`, new `.../workspace/components/AgentTodoPanel.tsx`.
- **Phase:** 12.3.
- **Notes:** new tests `ailienant-core/tests/test_phase12_3_integration_debts.py` (TODO1-8) and `ailienant-extension/src/test/agentTodoPanel.test.ts`. `SCHEMA_EVOLUTION.MD §42` records the wire contract.

### DEBT-041 [MEDIUM · RESOLVED 2026-06-20, 8.10.6] — GrepTool reads catalog-only files sequentially without a content index

- **Resolved:** added an FTS5 **trigram** line index (`file_lines`, stdlib `sqlite3`, feature-detected — no new dep) populated by `LazyIndexer` at index time. `GrepTool` gains a `narrow_provider` that lifts a safe literal from the pattern and pre-filters the catalog to a SUPERSET of true matches (RAM buffers + FTS hits + un-indexed index-lag files), then regex-confirms — so a match is never dropped. ReDoS bound: per-line input cap + wall-clock scan deadline that returns partial results. Narrowing activates when `GrepTool` is constructed with the provider (`make_fts_narrow_provider`); the index population is live now.
- **Date:** 2026-06-13
- **Reproduce:** `GrepTool._arun` iterates `path_provider()` and calls `content_reader(path)` per file via the firewalled `read_safe` reader. The mandatory O(max_matches) short-circuit limits total matches, but on a large workspace every pre-filter file still incurs a disk read until a match is found. No inverted index exists.
- **File(s):** `ailienant-core/tools/researcher_tools.py` (`GrepTool._scan`).
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §11.2)**. The `asyncio.to_thread` offload and the short-circuit guarantee the event loop is never blocked and latency is O(L) in the match cap. The residual is latency on very large workspaces with sparse matches.
- **Blocked by:** nothing structural; the enterprise fix adds an inverted content index and a ReDoS-bounded regex evaluator.
- **Phase:** Wave 2 / Analyst quality-lens (8.8.2), where search tooling becomes load-bearing for the Analyst.
- **Notes:** logged at 8.8.1 ship per CLAUDE.md §11.3.

### DEBT-039 [MEDIUM · RESOLVED 2026-06-29, 8.10.20] — Benchmark report artifacts have no retention policy

- **Resolved:** a configurable max-artifacts cap (default 20) with LRU-by-mtime eviction now runs at the write site. `core/benchmark/report.py` gains the pure `prune_artifacts(directory, max_runs)` (newest-N retained, only `*.json` candidates, tolerant of a vanished file, idempotent). `core/benchmark_service.py` reads `benchmark.max_stored_runs` from the global `~/.ailienant/.ailienant.json` (fail-safe to 20) and persists via `_persist_with_retention`, which serializes write+prune under an in-process `asyncio.Lock` plus a cross-process `filelock.FileLock` with all blocking I/O off the event loop (`asyncio.to_thread`). Durability-first: a lock timeout writes the report without pruning. Gate: `tests/benchmark/test_retention.py` (19 tests) including an end-to-end bound (5 runs · cap 3 → 3 artifacts).
- **Date:** 2026-06-13
- **Reproduce:** trigger `run_benchmark` repeatedly — each run writes a `~/.ailienant/benchmark/<task_id>.json` that is never pruned.
- **File(s):** `ailienant-core/core/benchmark_service.py` (`BENCHMARK_DIR`, `run_benchmark`).
- **Error:** not a runtime defect — unbounded disk growth over time. The single-flight cap bounds the *rate* of growth, not the total.
- **Blocked by:** nothing structural; needs a retention policy decision (cap by count, age-prune, or LRU eviction on write).
- **Phase:** standalone eval-surface hardening slice, post-8.5/8.8.
- **Notes:** logged at 8.5.5 ship per CLAUDE.md §7.3.

### DEBT-082 [MEDIUM · RESOLVED 2026-06-30, 8.13.4] — Bundled `@devcontainers/cli` not shipped in the `.vsix`

- **Date:** 2026-06-30 · **Resolved:** 2026-06-30 (8.13.4)
- **Reproduce (was):** pack the extension (`vsce package`) and install the `.vsix` on a machine with no `devcontainer` on PATH and the Dev Containers extension absent — `up()` degrades; the optional dep was present in `node_modules` for unpackaged dev runs only.
- **Error:** packaging gap, not a runtime defect — `@devcontainers/cli` is excluded from esbuild (it is a child-process bin, not an import), so `.vscodeignore`'s `node_modules/**` rule excluded it from the `.vsix`. In a packaged extension the CLI must come from PATH or the Dev Containers extension.
- **Resolution — host-prerequisite distribution model (option b):** the runnable `devcontainer` CLI is a documented **host prerequisite** (PATH or the Dev Containers extension `ms-vscode-remote.remote-containers`), not bundled. Chosen over bundling per CLAUDE.md §9 (lightest viable dependency; bundling the CLI's transitive runtime tree bloats the `.vsix` and widens the supply-chain surface), and because trusted execution already requires a local Docker daemon — requiring the CLI alongside it is reasonable. Concretely: `@devcontainers/cli` moved `optionalDependencies` → `devDependencies` (dev/test-only, never shipped); `DevcontainerProvisioner._doUp` now emits an actionable remediation (`CLI_MISSING_HINT`) on a `'path'`-source spawn and on an ENOENT failure, naming both supported ways to provide the CLI; `.vscodeignore` and `esbuild.js` are already the intended end state (unchanged). Documented in `PHASE_8.13_BLUEPRINT.md §3.5` + `DEVELOPERS.md`.

### DEBT-083 [LOW · RESOLVED 2026-08-03, 12.4] — Devcontainer exec output is buffered, not incremental

- **Date:** 2026-06-30 · **Resolved:** 2026-08-03 (12.4)
- **Detail (was):** the host handler (`providers/devcontainerExecHandler.ts`) ran `provisioner.exec()` to completion and emitted one `client_devcontainer_exec_stream` per stream plus the exit frame. Contract-equivalent, but long-running trusted commands did not stream live.
- **Resolution:** `DevcontainerProvisioner._spawnWithTimeout` gained an optional `onChunk` callback fired from the existing child `stdout`/`stderr` `data` listeners (accumulation into the final `ExecResult` unchanged, so a caller that ignores it sees byte-identical behavior); `devcontainerExecHandler.ts` wires a per-stream coalescer (`makeStreamCoalescer`) that batches rapid `data` events into WS frames on a ~50ms timer or an 8KB cap, flushing any residue before the exit frame (ordering is load-bearing — a frame arriving after the exit frame is dropped by `append_devcontainer_stream`'s unknown-`request_id` branch). Backend `ws_contracts.py`/`append_devcontainer_stream` unchanged — it already aggregated any number of chunks.

### DEBT-084 [MEDIUM · RESOLVED 2026-08-03, 12.4] — Interactive devcontainer sessions not wired over the host bridge

- **Date:** 2026-06-30 · **Resolved:** 2026-08-03 (12.4, plumbing + teardown only)
- **Detail (was):** `WebSocketHostBridge.open_host_session` raised `SandboxSessionError` unconditionally — the 8.13.4 WS contract covered one-shot exec only.
- **Resolution:** new §43 WS contract (`SCHEMA_EVOLUTION.MD`) — an 8-event session lifecycle (open/stdin/signal/flow/close/opened/stream/exit) keyed by `session_ref`. Backend: `core/command_boundary.py::CommandBoundaryFramer` extracts the sentinel-marker command-boundary protocol shared with `core/pty_session.py`; `api/devcontainer_bridge.py::_BridgeSandboxSession` implements `SandboxSession` over a manager-owned bounded queue instead of a reader thread; `ConnectionManager` gains the session primitives, including bidirectional backpressure detection (both `push_devcontainer_session_chunk` and `check_devcontainer_session_drain` check the pause/resume threshold — a producer-only check would deadlock once the queue fills and the child's own output has stalled, since nothing would push again to trip a resume). Host: new `providers/devcontainerSessionHandler.ts`, stateful (a `Map<session_ref, ChildProcess>`), no `node-pty` (see DEBT-139), an idle ceiling that self-terminates an orphaned session, and `dispose()` wired into the extension's teardown lifecycle. **Not wired to a production consumer** — `brain/agentic_cell.py` still uses the oracle tier; see DEBT-138 for the OCC precondition blocking that reroute. Exercised end-to-end via `tests/test_devcontainer_session_manager.py`, `tests/test_devcontainer_bridge.py`, `src/test/devcontainerSessionHandler.test.ts`, and the pre-existing `configurable["cell_adapter"]` test seam.

### DEBT-085 [LOW · RESOLVED 2026-08-03, 12.4] — Devcontainer exec ignores the backend `cwd`

- **Date:** 2026-06-30 · **Resolved:** 2026-08-03 (12.4)
- **Detail (was):** the host ran `devcontainer exec --workspace-folder <root>` at the container's workspace root; the backend `cwd` (a host path) was carried on the wire but never mapped into the container filesystem, so a sub-directory `working_dir` was silently ignored.
- **Resolution:** `DevcontainerProvisioner` resolves the container-side workspace root two-tier — first a best-effort parse of `up`'s own JSON result line (`remoteWorkspaceFolder`), falling back to one cached `pwd` probe on first use; a devcontainer with neither signal degrades to the pre-fix behavior (unprefixed, at the container root) rather than erroring. `devcontainerExecHandler.ts` translates `data.cwd` via `path.relative` against the workspace root, normalizes host separators to POSIX, and refuses to translate (runs unprefixed) on an empty/root-equal/traversal/cross-drive result — a confinement floor, not best-effort convenience. `exec()` prefixes `cd <single-quoted path> && ` inside the existing `/bin/sh -c` invocation; no new host-side shell-injection surface.

### DEBT-086 [LOW · RESOLVED 2026-08-03, 12.4] — Typecheck/validation helpers stay on the oracle tier

- **Date:** 2026-06-30 · **Resolved:** 2026-08-03 (12.4)
- **Detail (was):** `check_type_integrity` and `coder_tools._exec` ran trusted checks but took no `session_id`, so `resolve_execution_adapter` always kept them on the oracle tier rather than the devcontainer.
- **Resolution — non-interactive routing, not a naive session-id thread-through:** naively adding `trusted=True` would have regressed into a double HITL prompt, since the trusted resolver's default fallback (`NativeHITLSandboxAdapter`) raises an approval card unconditionally on every call — turning a silent validation check into a surprise prompt, and double-prompting `coder_tools._exec` on top of `_gated_exec`'s own consent round-trip. `core.sandbox.resolve_execution_adapter` gained an additive `interactive_fallback: bool = True` parameter; `False` resolves a second cached trusted adapter (`get_trusted_adapter_silent`) whose fallback is the oracle cage (`_OracleFallbackAdapter`, re-resolving `get_active_adapter()` per call rather than capturing it at construction) instead of the HITL-interactive adapter. Both helpers now call with `interactive_fallback=False`: `CheckTypeIntegrityTool` via a `PrivateAttr` session binding injected by a new state-reading `tool_registry.py` factory (mirroring `_read_file`'s pattern); `coder_tools._exec` via the `_SessionCtx` already carried by `_gated_exec`. Cage isolation was re-verified before adopting the silent fallback: `network_mode="none"`, read-only rootfs, `mode: "ro"` workspace bind-mount, non-root `USER sandbox` — `NativeHITLSandboxAdapter` was a redundant consent layer, not the isolation boundary, so suppressing it does not weaken the cage.

### DEBT-024 [MEDIUM · RESOLVED 2026-06-20, 8.10.6] — HITL inline-diff transport ships full file content (O(N)) instead of a unified diff (O(Δ))

- **Resolved:** `ProposedFile` carries a server-computed `unified_diff` (additive; `new_content` demoted to deprecated `Optional[str]=None`, §10-safe); `task_service` reads the old side via the VFS-safe reader, EOL-normalizes both sides, and emits a `difflib` unified diff. The host (`PatchActuator`) reconstructs via the `diff` library's `applyPatch`; an `applyPatch` failure (server-old vs host-old drift) degrades to a stale-file notice, with the base-hash OCC guard still authoritative on apply. The apply-write path (`WorkspaceEditItem` full content) is unchanged.
- **Date:** 2026-06-08
- **Files:**
  - `ailienant-core/api/ws_contracts.py` — `ProposedFile.new_content` (full post-edit content).
  - `ailienant-core/core/task_service.py` — HITL branch builds `proposed_files` from `pending_contents`.
  - `ailienant-extension/src/core/PatchActuator.ts` — `preview()` / `apply()` build `PatchedFileDiff` with full `old_content` + `new_content`.
- **Error:** not a type error — a space/latency trade-off. The pre-apply inline-diff approval (and the existing post-apply `RENDER_DIFF`) transport **full file content per file** over the WebSocket. For a large file this is O(N) on the wire and the client-side `diffLines(old, new)` is O(N) work; a multi-thousand-line file risks WS-buffer pressure and a main-thread/event-loop stall during diffing.
- **Blocked by:** nothing — but it must convert **both** the pre-apply (this fix) and the post-apply (`PatchActuator.apply` → `RENDER_DIFF`) paths together, since they share `PatchedFileDiff`/`DiffBlockShape`.
- **Phase:** future performance/transport sub-phase (own ticket — do not smuggle into a feature fix).
- **Notes:** declared MVP during the ASK-mode inline-approval fix (one-atomic-event design). Bounded by the existing `DIFF_RENDER_LINE_CAP=400` *mount* cap in `DiffBlock.tsx`, but the **wire payload** is still uncapped. Target design: compute the unified diff server-side with `difflib`, transport unified diffs only (O(Δ)), and reconstruct both sides in the host via the already-present `diff` lib (`applyPatch`). Safe to defer under the bounded-file-size assumption.

### DEBT-098 [MEDIUM · Floating] — Single ProcessPoolExecutor shared across all CPU-bound work

- **Date:** 2026-07-02
- **Reproduce:** `core/compute_pool.py:35-44` — one `ProcessPoolExecutor(max_workers=physical_cores-1)` instantiated at module scope and reused by every CPU-bound caller: GraphRAG PPR/analytics compute (`brain/memory.py`), the lazy indexer (`core/indexer.py`), and the blast-radius traversal (`core/blast_radius.py`, 8.14.1). No priority lanes or separate queues exist between them.
- **Error:** performance/contention issue, not a correctness defect. On a modest core count (e.g., 4 physical → 3 workers), a reactive-index write racing a blast-radius pre-apply check or a PPR recompute can starve each other with no ordering guarantee — the pool services requests FIFO with no notion of "this is a synchronous user-facing check, that is a background reindex."
- **Blocked by:** nothing structural; a fix could add either a priority-weighted submission wrapper over the existing pool, or split into a small foreground pool + a background pool, sized off the same `physical-1` budget.
- **Phase:** future performance slice — worth revisiting if/when 8.14.9's blast-radius stress gate (<500ms wall-clock target) starts flaking under concurrent indexer load.
- **Notes:** logged during a general bottleneck audit (2026-07-02); no reproduction of an actual contention failure yet — this is a structural risk, not an observed incident.
- **Decision (2026-08-04, 12.14 — CLAUDE.md §4 Option B, Manifest Update):** explicit defer, reviewed
  rather than silently carried forward. Real but low-incidence at current scale — no reproduction of an
  actual contention failure exists as of this review. Revisit trigger is unchanged and concrete: the
  8.14.9 blast-radius stress gate starting to flake under concurrent indexer load. Not required for
  Phase 13.


---

### DEBT-173 [LOW · RESOLVED 2026-08-25, 13.1.1] — `/init`'s generated-file fork wasn't covered by an already-provisioned workspace's `.gitignore`

- **Resolved:** 2026-08-25. Re-verified: `ensureGitignoreBlock` was still gated behind `PROVISIONED_FLAG`'s one-time early return. A second, deeper gap surfaced on inspection: `GITIGNORE_BLOCK` had never carried a pattern for `*.generated.md` at all — even a brand-new first-time provision would have missed ignoring the `/init`-generated fork, not just an already-provisioned workspace. Fixed both in one pass: added `*.generated.md` to `GITIGNORE_BLOCK_BODY`; extracted the block-diffing logic into a pure `computeNextGitignore` function (absent → append, current → no-op, STALE → heal in place rather than duplicate); moved its call outside the `PROVISIONED_FLAG` gate so it re-checks (cheaply, as a no-op once current) on every activation, not just the first.

- **Date:** 2026-08-17
- **Reproduce:** `ensureGitignoreBlock` (`ailienant-extension/src/workspace_provisioning.ts`) only runs the marker check-and-append once, gated behind the same `PROVISIONED_FLAG` as the rest of first-run provisioning (`provisionWorkspaceHome`). A workspace that was provisioned before `/init` shipped already has its `.gitignore` block written and will never re-run `ensureGitignoreBlock` — so `AILIENANT.generated.md` (the sibling `/init` writes when `AILIENANT.md` already has user content, `core/project_init.py::_resolve_target`) is absent from the ignore list on any such workspace, and a careless `git add .` could commit it.
- **File(s):** `ailienant-extension/src/workspace_provisioning.ts` (`GITIGNORE_BLOCK`, `ensureGitignoreBlock`), `ailienant-core/core/project_init.py` (`_GENERATED_SUFFIX`).
- **Error:** tooling gap, not a correctness defect — the file is meant to be reviewed and merged/discarded by the user, not committed as-is, so the failure mode is "clutters a commit," not data loss or silent behavior change.
- **Blocked by:** nothing — the fix is re-running (or additively re-checking) `ensureGitignoreBlock` on every activation instead of only pre-`PROVISIONED_FLAG`, which was deliberately out of scope for this session (touching the provisioning idempotency gate warranted its own review).
- **Phase:** future provisioning-refresh slice.
- **Notes:** logged at ship per CLAUDE.md §11.3, alongside the `/init` feature itself (2026-08-17).
### DEBT-179 [LOW · RESOLVED 2026-08-25, 13.1.2] — App-runtime Docker image shipped dev/test tooling alongside production dependencies

- **Date:** 2026-08-18 · **Resolved:** 2026-08-25 (13.1.2)
- **Was:** the root-level `Dockerfile` installed `ailienant-core/requirements.txt` verbatim — dev/lint/test tooling (`pytest`, `pytest-cov`, `mypy`, `ruff`, `pre-commit`, `hypothesis`) was interleaved with runtime dependencies, so the shipped image carried all of it, unused at runtime.
- **Resolved:** split the six dev-only pins into a new `ailienant-core/requirements-dev.txt` (`-r requirements.txt` plus the six tools); `requirements.txt` now lists runtime dependencies only. The `Dockerfile` needed no change — it already installs only `requirements.txt`. Updated the three places that expected the old single-file setup: `.github/workflows/backend-gate.yml` (installs `requirements-dev.txt`, which pulls both), `CONTRIBUTING.md`'s contributor setup, and `scripts/pre_commit_backend_gate.py`'s missing-tool message.
- **File(s):** `Dockerfile` (unchanged), `ailienant-core/requirements.txt`, `ailienant-core/requirements-dev.txt` (new), `.github/workflows/backend-gate.yml`, `CONTRIBUTING.md`, `scripts/pre_commit_backend_gate.py`.
- **Verified:** `docker run ailienant-backend:local pip list` confirms `pytest`/`mypy`/`ruff`/`hypothesis`/`pre-commit` are absent from the image; `docker compose up` reaches `healthy`.


### DEBT-180 [LOW · RESOLVED 2026-08-25, 13.1.1] — Socratic grill's agreement detector did substring matching, not intent matching

- **Resolved:** 2026-08-25. Re-verified the exact false-positive: `_is_agreement("Yes, establish component files for Header, HeroSlider...")` returned `True`. Replaced the unanchored `any(signal in text for signal in _AGREEMENT_SIGNALS)` with a clause-anchored check: the whole message must equal a signal, OR every comma/period-separated clause must independently be one. The clause form is load-bearing, not cosmetic — the frontend's own canonical plan-acceptance phrase (`AGREEMENT_SIGNAL = 'Looks good, proceed.'` in `Workspace.tsx`) is two signals joined by a comma, and a first draft using strict whole-message equality broke that exact contract (caught by the pre-existing `test_free_text_agreement_still_short_circuits_on_a_fresh_turn` regression test before it shipped). A second, wider full-suite run then caught a further gap the local analyst-test files hadn't: `test_ideation.py` and `test_ideation_handoff_contract.py` both pin `"looks good, let's proceed"` as agreement — "let's proceed" is neither a literal signal nor two comma-split signals. Added a small, fixed leading-filler strip (`"let's "`, `"please "`, …) applied per clause before the signal-set lookup, so a filler-plus-signal clause still matches without loosening the lookup itself back into a substring search.

- **Date:** 2026-08-18
- **Reproduce:** `agents/analyst.py::_is_agreement` (`agents/analyst.py:85-88`) checks `any(signal in text for signal in _AGREEMENT_SIGNALS)` against the user's raw lowercased reply. Short tokens in `_AGREEMENT_SIGNALS` ("ok", "yes", "bien") match as a substring of any longer answer — e.g. "Yes, establish component files for Header, HeroSlider..." ends the grill on the first word instead of being read as a substantive, still-elaborating answer.
- **File(s):** `agents/analyst.py` (`_is_agreement`, `_AGREEMENT_SIGNALS`).
- **Error:** correctness gap, not a crash — the dialogue can close a turn earlier than the user intended, but never breaks the handoff itself.
- **Phase:** future ideation-quality slice — likely an LLM-based agreement classifier or an anchored match (start/end of string, or whole-message equality against a short allowlist) rather than unanchored substring search.
- **Notes:** found while root-causing the ideation→planner handoff regression fixed this same session (`docs/DEV_JOURNAL.md`, 2026-08-18 entry); not the cause of that regression, logged separately per CLAUDE.md §11.3. **Blast radius narrowed by 13.0.5** — the batched grill made the model's own empty-batch response the primary completion signal, so `_is_agreement` now only runs as a fast path on a genuinely fresh top-level turn (`grill_round_count == 0`), never between the internal `interrupt()`-driven rounds where a substantive answer is most likely to trip it. Still reachable on that first turn, so the entry stays open.
### DEBT-182 [MEDIUM · RESOLVED 2026-08-18, 13.0.5] — Tool field-level `Field(description=...)` never reached the LLM

- **Date:** 2026-08-18 · **Resolved:** 2026-08-18 (13.0.5)
- **Was:** this project uses prompt-enforced JSON rather than native function-calling (the gateway returns plain text — see `core/tool_dispatch.py`'s module docstring), so `build_schema_hint` is the *only* place a tool's schema is described to the model in the shared dispatch loop (`agents/coder.py`, `agents/analyst.py`, `agents/researcher.py`, `brain/nodes/subagent_worker_node.py`, all via `make_gateway_reasoner`). It rendered `- name(arg1, arg2): <first docstring line>` — argument NAMES only, from `model_json_schema().get("properties", {}).keys()`. Every `Field(description=...)` was silently discarded, and nested models were never expanded (Pydantic v2 emits them as a `$ref` into `$defs`, never inlined). Any tool author writing per-field LLM guidance had it dropped with no error and no warning.
- **Surfaced by:** 13.0.4's `AskUserQuestionInput.questions` batch field. Its descriptions ("2-4 mutually exclusive options", "mark exactly one recommended", "batch related questions") were the whole mechanism intended to fix the observed single-option behavior — and the model never saw a word of them. Live testing showed no behavior change at all, which is what led here.
- **Fix:** `build_schema_hint` now renders each property's own `description` and recurses through `$ref`-linked nested schemas (resolving against `$defs`, handling direct/`items`/`anyOf` positions, with a `seen` guard against self-reference). The existing `- name(args): desc` header line is unchanged, so a flat tool with no field descriptions renders as before.
- **Files:** `core/tool_dispatch.py` (`build_schema_hint`, new `_describe_schema_properties`/`_find_ref`/`_resolve_ref` helpers).
- **Verified:** `tests/test_tool_dispatch.py` — 2 new cases (flat tool renders header + its field description; `AskUserQuestionTool` surfaces the top-level `questions` description and recurses two levels into `AskUserQuestionItem`/`AskUserQuestionOptionInput`). `mypy .` 0/469 · `pyright` 0 · full `pytest` green.
- **Notes:** the general class of bug is what matters here, not the one tool that exposed it — every future tool's field-level guidance was affected. `brain/agentic_cell.py` is unaffected: it has its own hardcoded reasoner over a fixed 3-tool set, not this renderer.

### DEBT-181 [MEDIUM · RESOLVED 2026-08-18] — Long Socratic grills silently lost pre-compaction history

- **Date:** 2026-08-18
- **Reproduce (pre-fix):** `brain/summarizer.py::run_summarize_node` compacts `state["messages"]` once the turn count/token estimate crosses its threshold, replacing older turns with a single `role="system"` `"[HISTORY SUMMARY]: ..."` entry. Both the analyst's question-replay site and `brain/ideation.py::_dialogue_transcript` filtered the replayed history to `role in ("user", "assistant")` only, so the injected summary was silently dropped — after compaction, the analyst's next question and the synthesis distillation both lost everything before the last few raw turns.
- **File(s):** `brain/summarizer.py` (`run_summarize_node`, new public `HISTORY_SUMMARY_PREFIX`), `agents/analyst.py` (the question-replay site, `_stream_question_llm` at the time — since 13.0.5 the same fold-in lives in `_build_grill_llm_messages`), `brain/ideation.py` (`_dialogue_transcript`).
- **Fix:** `HISTORY_SUMMARY_PREFIX` is now a shared, exported constant tagging the summary entry. The analyst's replay folds a matching system-role entry into the leading system message (one system turn, not a duplicate mid-transcript one) instead of dropping it; `_dialogue_transcript` renders it as an `EARLIER CONTEXT:` line in the flat transcript. Both readers key on the exact prefix, not a bare `role == "system"` check, so an unrelated future system-role entry in the same channel is not accidentally swept in.
- **Verify:** `pytest tests/test_ideation.py -k "summary"` — 2 tests, both green (the analyst-side one retargeted to `_build_grill_llm_messages` by 13.0.5, same asserted fold-in behavior); full `pytest`/`mypy .`/`npx pyright` all green (see `docs/DEV_JOURNAL.md`, 2026-08-18 entry).
- **Notes:** found while root-causing the ideation→planner handoff regression fixed the same session; fixed immediately rather than deferred, per explicit instruction.

### DEBT-183 [MEDIUM · RESOLVED 2026-08-20, 13.0.7] — Coder Companion explains only coding turns, once, after the fact

- **Date:** 2026-08-19 · **Resolved:** 2026-08-20 (13.0.7)
- **Was:** `brain/coder_companion.py::_run_coder_companion` had a single call site (`agents/coder.py`), a patch-shaped input contract (`_build_companion_request` reads `pending_patches`/`pending_contents`), and emitted one terminal WS blob (`CoderCompanionPayload`) that `workspaceStore.ts` replaced by `task_id` — no streaming/append semantics. `Workspace.tsx` hard-gated the card on `diffBlocks.length > 0`, so it structurally could not render outside a coding turn with a diff.
- **Fix:** two-part, matching the brief's proposed Timeline=what/Companion=why split. (1) The "step by step" half was already substantially solved by the Glass-Box Timeline; the real gap turned out to be that only the FIRST reasoning entry of a turn ever rendered (`AgentTimeline.tsx`'s `!reasoningRendered` latch) even though the data for every entry was already accumulated — fixed at zero new LLM cost by rendering every `reasoning` entry independently. (2) The Companion's input contract gained `scope`/`scope_summary` (additive), three new per-scope request builders each consuming only their own decision point's slice, and now fires at four real graph decision points (a grill round closing, a plan committing, a patch landing, error correction resolving) instead of once. Frontend storage moved to message-scoped append (`Message.companions`), and the `diffBlocks.length > 0` gate was dropped.
- **Verify:** `docs/DEV_JOURNAL.md` 2026-08-20 (13.0.7) entry; `mypy .` 0/469 · `pyright` 0 · `pytest` green (33 companion-scope tests) · `npm test` 236 passed.
- **Notes:** the design brief's five open questions were resolved in favor of: per-decision-point (not per-step, not per-phase) Companion granularity; Timeline/Companion boundary held exactly as proposed; cost governance reused unchanged plus a new shared emission cap; frontend delivery via message-scoped append, same visual position as before. See DEBT-186/187 for what was deliberately deferred out of this resolution.

### DEBT-186 [LOW · Floating] — `CoderCompanion*` naming survives its 13.0.7 generalization

- **Date:** 2026-08-20
- **Was/is:** `CoderCompanionPayload`, the `server_coder_companion` WS event, `brain/coder_companion.py`, and `CoderCompanionCard.tsx` all still carry the coding-specific name, even though 13.0.7 generalized the companion to explain ideation/planning/healing decision points too. A rename to `AgentCompanion*` was evaluated during 13.0.7 and deferred: it buys nothing functional and costs a §10-exception coordinated rename (the event is dispatched by string literal in `useWSMessageHandler.ts` and listed in the WS master union), better done with a deliberate dual-emit window than folded into an already-large batch.
- **File(s):** `brain/coder_companion.py`, `api/ws_contracts.py` (`CoderCompanionPayload`, `ServerCoderCompanionEvent`), `ailienant-extension/src/api/contracts.ts`, `ailienant-extension/src/workspace/components/CoderCompanionCard.tsx`, `ailienant-extension/src/workspace/hooks/useWSMessageHandler.ts`.
- **Phase:** opportunistic — rename with a dual-emit window (old event name kept alongside the new one for one release) whenever this area is next touched substantively.


### DEBT-187 [LOW · Floating] — Planning-scope companion `emission_id` doesn't distinguish a replan from the first plan

- **Date:** 2026-08-20
- **Was/is:** `agents/planner.py`'s companion scheduling call always passes `attempt_ordinal=0`, so `emission_id` is `f"{task_id}:planning:0"` regardless of how many times `run_planner_node` produces a committed plan within one task. If a future feature allows a mid-task replan, the second plan's companion entry would collide on the same `emission_id` and silently replace the first in the frontend's append store instead of adding a new entry — no state counter exists today to disambiguate, since the planner has never needed one (each task currently plans exactly once).
- **File(s):** `agents/planner.py` (the `schedule_agent_companion("planning", 0, ...)` call site).
- **Phase:** only relevant if/when a mid-task replanning feature ships; add a `planning_attempt_count`-style state channel at that point, mirroring `grill_round_count`'s role for the ideation scope.


### DEBT-184 [HIGH · RESOLVED 2026-08-19, 13.0.6] — HITL_RESPONSE host bridge silently dropped every clarification-answer field

- **Date:** 2026-08-19 · **Resolved:** 2026-08-19 (13.0.6)
- **Was:** `providers/workspace_panel.ts`'s `HITL_RESPONSE` case forwarded only `{approval_id, approved, comment, modified_content}` to `client_hitl_response` — the exact four fields the ORIGINAL (pre-DEBT-171) approve/reject contract had. `answer`, `selected_option`, and `answers` (added additively across 13.0.1/13.0.4 on `HITLResponsePayload`, `useHitlResponder.ts`, `useClarificationResponder.ts`) were never added to this one whitelist. Every option a `ClarificationGrillCard` answer picked was silently discarded at this single bridge point — the WS contract, the resume path, and every backend test all round-tripped correctly in isolation, which is exactly why nothing caught it: the gap lived entirely in untested host-bridge glue between two components that were each individually correct.
- **Symptom:** answering a clarification card appeared to work (card dismissed, request sent) but the backend always resumed with `answers=None`, folding to `"(the operator gave no answer)"` — reported by the user as the grill "getting stuck" after answering.
- **Fix:** added the three fields with a conditional spread (absent stays absent, never an explicit `undefined`), extracted into an exported pure function (`buildHitlResponseData`) specifically so it can be unit-tested without a live webview panel — a schema check alone (`contracts.ts`, `ws_contracts.py`) cannot catch a field silently dropped mid-bridge; only a fixture on the bridge function itself can.
- **File(s):** `ailienant-extension/src/providers/workspace_panel.ts`.
- **Verified:** new `src/test/hitlResponseBridge.test.ts` (5 cases, run inside the real extension host per this suite's convention — `workspace_panel.ts` imports `vscode`, which only resolves there, not in a plain Node/jsdom context); `npm test` 208 passed.
- **Notes:** found while investigating the "grill answers get lost" report; the two DEBT-171/13.0.4 authors (also this agent, in prior sessions) never traced the reply path all the way through the host bridge — every other link in the chain was verified, this one wasn't.

### DEBT-185 [HIGH · RESOLVED 2026-08-19, 13.0.6] — `analyst_grill` generated its question batch and called interrupt() in the same invocation

- **Date:** 2026-08-19 · **Resolved:** 2026-08-19 (13.0.6)
- **Was:** `agents/analyst.py::run_analyst_node` (as shipped in 13.0.5) composed a question batch via `_generate_grill_questions_llm` (temperature 0.2) and then called `_resolve_grill_answers`/`interrupt()` in the SAME node invocation. LangGraph replays a node from the top on every resume, so the LLM call regenerating the batch re-ran on every resume too — a resumed round could receive a *different* batch than the one the operator actually answered. Ids are positional (`q{i}`), so a differently-sized replay batch silently misaligned answers to the wrong questions with no mismatch to detect; a replay batch that happened to come back empty skipped `interrupt()` entirely and discarded the resume value outright. `brain/agentic_cell.py` documents the opposite invariant in three places (its `pending_exec_command`/`pending_tool_call`/`pending_hitl_request` defer-then-interrupt-first phases) specifically to avoid this class of bug; `analyst_grill` violated it.
- **Symptom:** reported by the user as the interview hanging indefinitely after submitting an answer — compounded by DEBT-184 (no visible progress either, since the replay burned several silent model round-trips) and the token-only busy indicator (below).
- **Fix:** split the node into two graph super-steps via a new declared state channel, `pending_grill_batch` (`SCHEMA_EVOLUTION.MD §53`): a generate phase commits the batch to state and returns with no interrupt; the ask phase (the self-loop's next visit) resolves answers against the state-sourced batch as its FIRST action, so a resume only ever replays the interrupt call itself, never the LLM call that produced the batch.
- **File(s):** `brain/state.py` (`pending_grill_batch`), `agents/analyst.py` (`run_analyst_node` split, `_render_batch_for_history` retargeted to the serialized dicts).
- **Verified:** `tests/test_analyst_agent.py` (2 new regression guards: the generate phase never calls the clarification seam; the ask phase never regenerates the batch — patches `_generate_grill_questions_llm` to raise if called), plus the full existing suite retargeted to drive both phases where a round completes. `tests/test_engine_respine.py`'s ideation case (unrelated pre-existing gap: its own DEBUG stub was never actually enabled, `DEBUG_MODE` being a module constant read at import) now exercises the real end-to-end card path. `mypy .` 0 · `pyright` 0 · full `pytest` green.
- **Notes:** the accepted "grounding re-runs once on replay" cost noted in 13.0.5 stays true and bounded (each round's own grounding replays at most once, on its own ask-phase resume) — this fix only removes the LLM call for QUESTION GENERATION from the replay path, which is where non-determinism actually caused a correctness bug rather than just a wasted call.

### DEBT-188 [HIGH · RESOLVED 2026-08-20, 13.0.8] — `client_hitl_response` resumed by WS connection identity, not chat session — silently stranded a paused graph

- **Date:** 2026-08-20 · **Resolved:** 2026-08-20 (13.0.8)
- **Was:** `main.py`'s `client_hitl_response` handler called `task_service.has_paused_graph(client_id)` / `resume_graph(client_id, ...)` / `register_active_task(client_id, ...)`, using `client_id` — the WS route's path parameter, stable for the whole physical connection — while `TaskService._paused_tasks` is actually keyed by the chat's own `session_id` (the value `_run_coding_task` was invoked with). `register_alias`/`RegisterSessionPayload` already support several sessions sharing one connection ("one panel announcing its session id on the shared connection"), and `HITLResponsePayload` carried no `session_id` field at all — so the two only ever coincided when exactly one session was active on a connection. Once a second session shared it, the resume lookup missed silently and fell through to `resolve_human_approval` (the *other* HITL transport, for the in-memory-Event/MCP-adapter path — a no-op for a native LangGraph interrupt), and the paused graph was never resumed.
- **Symptom:** reported by the user as a Plan-mode grill hanging for hours after answering, with VS Code eventually surfacing "backend unreachable." Root-caused with certainty from the user's own `.ailienant_telemetry.log`: three session ids registered within ~20 minutes on one connection (the user retrying the same prompt in a fresh session after each apparent hang — the first grill almost certainly hit the identical bug), a `server_hitl_ack` for both answered rounds stamped with a fourth id that never appears in any `client_register_session` event, and — the clinching evidence — three later `client_abort_mesh` calls for the real session each returning `signalled=false` (nothing was ever registered to abort, because the resume never started). Two Explore passes first cleared the 13.0.7 Companion generalization (fire-and-forget call sites, symmetric semaphore/emission-cap, no event-loop-blocking call) before the log made the actual mechanism explicit.
- **Fix:** added `session_id: Optional[str] = None` to `HITLResponsePayload` (additive); `workspace_panel.ts`'s `buildHitlResponseData` now stamps the panel's own `session.id`; `main.py` resolves via a new, directly-unit-tested `_resolve_hitl_session_id(data, client_id)` (falls back to `client_id` only for a stale pre-fix webview) for every one of the four call sites above.
- **File(s):** `api/ws_contracts.py`, `main.py`, `ailienant-extension/src/providers/workspace_panel.ts`, `ailienant-extension/src/api/contracts.ts`.
- **Verified:** new `tests/test_hitl_session_routing.py` (models the exact log scenario: two sessions sharing one connection resume independently; the pre-fix bare-`client_id` lookup is shown missing both); extended `hitlResponseBridge.test.ts`. `mypy .` 0/470 · `pyright` 0 · full `pytest`/`npm test` green.
- **Notes:** two adjacent angles the user asked to rule out — session lookup by name/title, and a new session orphaning an old paused one's WS listener — were both checked and found to be false leads (title is never a lookup key anywhere; the architecture is one webview panel per session, each with its own permanently-closed-over `session.id` and live listener, so N sessions already receive background updates concurrently). Not caused by 13.0.7 — see DEBT-189 for the second bug fixed in the same batch.

### DEBT-189 [MEDIUM · RESOLVED 2026-08-20, 13.0.8] — Active-task header/spinner lost on any tab switch, pre-existing since Phase 11.6

- **Date:** 2026-08-20 · **Resolved:** 2026-08-20 (13.0.8)
- **Was:** `providers/workspace_panel.ts` sets `retainContextWhenHidden: false` deliberately (state survives via `acquireVsCodeApi().setState/getState`, so the DOM itself doesn't need to stay resident) — VS Code fully destroys the webview's JS context on hide and reconstructs it on reveal. `activeTaskPrompt`/`activeTaskStartedAt`/`isTurnActive` (`chatStore.ts`) are memory-only by design and were never in `workspaceStore.ts`'s persisted whitelist nor in the existing `REHYDRATE_TRANSCRIPT` restoration path (which only restores the message transcript) — so they silently reset to their defaults on every hide→reveal cycle, regardless of whether a task was still genuinely running.
- **Symptom:** reported alongside DEBT-188 — after switching tabs and returning, both the sent prompt and its loader had disappeared, leaving no way to tell whether the task was cancelled or was still executing.
- **Fix:** `_runningTasks` (host memory, survives the teardown) changed from a bare `Set<string>` to a `Map<string, {prompt, startedAt}>`, populated at `SUBMIT_TASK` and re-armed on a `HITL_RESPONSE` reply; a new `_pendingHitlSessions` set stops the stream_end/task_complete cleanup from clearing the marker for a `server_stream_end` that only reflects a pause-on-interrupt (astream ends "naturally" on a native interrupt too, so a paused task would otherwise look finished the instant its clarification card appeared). A new `ACTIVE_TASK_RESTORED` host→webview message posts on panel reveal, alongside the existing plan/transcript re-posts, restoring the three chatStore fields.
- **File(s):** `ailienant-extension/src/providers/workspace_panel.ts`, `ailienant-extension/src/workspace/hooks/useWSMessageHandler.ts`.
- **Verified:** new `src/test/activeTaskRestored.test.ts` (restores on a well-formed payload; no-ops on a malformed one). `npm test` 240 passed.
- **Notes:** confirmed pre-existing since commit `0c8744a` (Phase 11.6, 2026-07-27) via `git show` on the 13.0.7 commit — none of its diff lines touch `activeTaskPrompt`/`activeTaskStartedAt`/`isTurnActive`/`retainContextWhenHidden`. Simply never exercised by a hide/reveal cycle in prior live testing.

### DEBT-190 [MEDIUM · RESOLVED 2026-08-20, 13.0.8] — Ideation-scope Companion always degraded on a local-only BYOM setup (queued behind the grill's own next local call)

- **Date:** 2026-08-20 · **Resolved:** 2026-08-20 (13.0.8)
- **Was:** `_companion_gpu_slot_available` (13.0.7) reads `GPUResourceManager`'s cooperative VRAM lock to avoid contending with the user's real local-model work — but `agents/analyst.py`'s own grill LLM calls (grounding + question generation) never acquire that lock at all; only the coding path's local generation does. So the probe always saw "nobody holding the lock" during a grill round and admitted the companion, which then raced the grill's own immediately-following local LLM call for the same underlying inference server. Confirmed empirically: a single solo call to the resolved judge tier's local Ollama target (`qwen2.5-coder:3b`) took ~11.4s; two concurrent calls to the same model took 28.4s total — Ollama serializes requests to one model regardless of AILIENANT's own bookkeeping. On real (larger) grill prompts this reliably pushed the companion's call past its 45s local-tier timeout, so it degraded on every round rather than rarely.
- **Symptom:** reported by the user as "One explanation was unavailable." appearing in chat after every grill answer, escalating to "2 explanations were unavailable." after a second round — i.e. every ideation-scope companion emission degrading, not an occasional one.
- **Why the coding path is unaffected:** `schedule_coder_companion` fires only after the coder's own local generation call has already completed (and released the lock) — by design there's no concurrent local call in flight at that point, so the same race never occurs there.
- **Fix:** new `_ideation_companion_would_contend_local_compute()` — resolves the judge tier via `get_chat_target`/`is_local` and, when true, skips the ideation-scope emission entirely *before* attempting the call (no LLM call, no broadcast at all — not even a degraded one), scoped to `scope == "ideation"` only so planning/healing (which don't share the grill's self-loop's immediate local-call handoff) are untouched.
- **File(s):** `brain/coder_companion.py`.
- **Verified:** reproduced directly against the real local BYOM environment (not mocked) before and after the fix — confirmed the pre-fix call succeeds in isolation (~11s, no contention) but the full two-round scenario now produces zero broadcasts instead of two degraded ones. New tests in `tests/test_coder_companion.py` (probe true/false/unresolved/fail-open; ideation skips cleanly when local; ideation proceeds when cloud; coding path never consults the new probe). `mypy .` 0/470 · `pyright` 0 · full `pytest` green.
- **Notes:** found and fixed live, same session as 13.0.8's shipping — folded into that entry rather than a new manifest line since 13.0.8 was still uncommitted when this surfaced.

### DEBT-191 [MEDIUM · RESOLVED 2026-08-20, 13.0.8] — Flat 300s local-model timeout couldn't fit a demanding structured-generation call on slow hardware

- **Date:** 2026-08-20 · **Resolved:** 2026-08-20 (13.0.8)
- **Was:** `tools/llm_gateway.py`'s `_LOCAL_LLM_TIMEOUT_S = 300.0` was litellm's per-request timeout for any resolved-local BYOM target, applied identically at every local call regardless of `max_tokens` or the actual hardware serving it. Measured directly on real constrained hardware: ~2-3 tokens/sec — a flat 300s budget covers barely 600-900 tokens, nowhere near the planner's real `MissionSpecification` draft (sized up to several thousand output tokens for a broad request), so every one of 3 retry attempts produced an empty, unparseable response.
- **Symptom:** reported by the user as the planner failing with "Planner Error - schema validation exhausted 3 attempts: 6 validation errors for MissionSpecification ... input_value={}" — every required field missing, because the LLM response was genuinely empty/unparseable on every attempt, not a logic bug in the schema-extraction code.
- **Fix:** replaced the flat constant with `resolve_local_timeout(max_tokens, model)` — a static `max_tokens`-scaled formula (`max(300s floor, max_tokens × 0.5s/token + 60s cushion)`, all three knobs env-overridable) as the seed for a model with no history, refined by a per-model adaptive calibration layer once a model has accumulated ≥2 completed local calls totaling ≥200 real `completion_tokens` (the two-axis gate — sample count AND total tokens — closes a real bug found live: two tiny 3-token samples alone, dominated by fixed per-request overhead rather than steady-state generation, produced a ~3.6-hour timeout estimate before this gate existed). Calibration recorded per resolved model string (not globally — different tiers can be genuinely different-speed models), duration-weighted (total duration ÷ total tokens across the window, not an average of per-call ratios), times a 1.3x safety margin, never below the static floor. Only `ainvoke` records samples (the chokepoint the planner/coder/mini-judge/companion all share); `acomplete_byom`/`astream_byom`/`astream_byom_thinking` read calibration data if it exists but don't contribute their own — see DEBT-192.
- **Follow-up findings from a second, deeper audit pass** (the user explicitly asked for one before considering this closed): (1) a real regression — `tests/test_llm_gateway_timeout.py` imported the now-deleted `_LOCAL_LLM_TIMEOUT_S` name, aborting collection for its 3 tests; rewritten with 16 tests total, including full coverage of the calibration gates, per-model isolation, and the floor. (2) A real, unacknowledged timing bug — the response_format-strip-and-retry branch reused the first (wasted) attempt's start time, inflating a calibration sample with time that didn't produce the recorded tokens; fixed by resetting the timer before the retry, with a dedicated test using real (not mocked) timing deltas to prove it. (3) A confirmed test-isolation gap — the new module-level calibration dict had no reset fixture, unlike its direct precedent (`_companion_emission_counts`); added an `autouse` fixture. (4) Retries multiply the worst case for a local timeout — confirmed via litellm/OpenAI-SDK source: `timeout`/`max_retries` are handed straight to the SDK's retry loop, which re-issues the *full* request at the *full* timeout each time, and `litellm.exceptions.Timeout` is retried by default. A local timeout means the hardware is slow or dead, not a transient network blip retrying could fix — reduced local-target retries from `LLM_MAX_TRANSPORT_RETRIES` (2) to 1 (env-overridable via `AILIENANT_LOCAL_LLM_MAX_RETRIES`), capping the worst case at 2x the resolved timeout instead of 3x. (5) `brain/agentic_cell.py`'s ReAct-loop per-turn ceiling (`AGENTIC_CELL_MAX_ELAPSED_S`, checked only *after* each iteration completes, never preemptively) was implicitly kept self-consistent by the old flat 300s LLM timeout — a single call could never itself exceed the whole turn's budget. The scaled timeout breaks that alignment, so `resolve_local_timeout` was made public and a new `_cell_elapsed_floor()` helper ensures the per-turn default is never smaller than what the cell's own single LLM call (with its own retry allowance) could legitimately need; an explicit `cell_max_elapsed_s` override still wins untouched.
- **File(s):** `tools/llm_gateway.py`, `brain/agentic_cell.py`, `tests/test_llm_gateway_timeout.py`, `tests/test_phase7_19_3_iteration_governor.py`.
- **Verified:** reproduced directly against the real local BYOM environment throughout (not mocked) — a solo call, two concurrent calls proving Ollama serializes requests, the full pipeline with a realistic prompt, and the calibrated end-to-end result after all fixes landed (654.7s for a real 2-sample/435-token calibration, sane and non-inflated). 16 new/rewritten tests in `test_llm_gateway_timeout.py`, 2 new tests in `test_phase7_19_3_iteration_governor.py` (the existing `test_time_axis_exhausted` already regression-guards the "explicit override still wins" half). `mypy .`/`pyright`/`ruff` clean, full `pytest` green.
- **Notes:** two audit passes on this fix specifically (the user pushed back twice — "analyze deeper" both inside and outside the immediate diff) is what surfaced findings (1)-(5); none of them were visible from the ad-hoc live `python -c` verification used while writing the original fix, only from an independent re-read plus an actual full-suite `pytest` run.

### DEBT-192 [LOW · Floating] — Local-model calibration is recorded only from `ainvoke`, not from the direct-BYOM streaming paths

Only `ainvoke` (`tools/llm_gateway.py`) contributes samples to the per-model calibration window DEBT-191 introduced; `acomplete_byom`/`astream_byom`/`astream_byom_thinking` (the main-chat streaming paths) read calibration data if it exists (so they still benefit once `ainvoke` has calibrated a shared model) but never write their own. A model driven exclusively through main-chat streaming never self-calibrates on its own traffic. Deliberate, confirmed with the user: the reported bug and the highest-risk failure mode (a structured-JSON call silently degrading to an empty response) both live behind `ainvoke`; the streaming paths already show the user live token arrival, so an under-calibrated timeout there is a slower stream, not a silent dead failure. Revisit if chat streaming on slow local hardware turns out to need the same treatment.


### DEBT-193 [LOW · Floating] — A local-model timeout never triggers failover, unlike a connection drop

`litellm.exceptions.Timeout` is not a subclass of `litellm.exceptions.APIConnectionError` (confirmed via litellm's own exception hierarchy), so the `except APIConnectionError` blocks in `tools/llm_gateway.py` (`ainvoke`, `acomplete_byom`, `astream_byom`) never catch a local timeout — only a genuine connection drop or CUDA OOM gets the local→next-target failover / cloud cascade those blocks provide. A local target that times out (as opposed to dropping the connection) just re-raises after its retry budget (DEBT-191) is exhausted, with no attempt to fail over to an alternate configured target. Pre-existing, unrelated to DEBT-191's own purpose (sizing the timeout, not routing around a dead endpoint) — found during DEBT-191's own audit, not fixed there.


### DEBT-195 [LOW · Floating] — Other flat, hardware-blind timeout constants surveyed during DEBT-191's audit

Assessed and left alone (different subsystem or genuinely hardware-independent by design): `core/benchmark/codegen.py`/`core/benchmark/oracle.py`'s 30-300s sandbox-execution timeouts bound running the *candidate program*, not the LLM call itself — same flat-number shape in spirit as the original DEBT-191 bug, but a different subsystem (the eval/benchmark harness, not the interactive product path), lower priority. One latent, off-the-primary-path gap: `ainvoke`'s legacy non-BYOM proxy branch (hit only when `effective_model` doesn't start with `"ailienant/"`, the back-compat litellm-proxy path) uses the caller's raw `timeout` with no `resolve_local_timeout` call at all — not a live bug today since it's off the default path, but a latent instance of the same class if that branch is ever hit against a genuinely local target.


### DEBT-196 [LOW · RESOLVED 2026-08-25, 13.1.1] — HUD/telemetry (token speedometer) had no host-side mirror or rehydration message

**Resolved:** 2026-08-25. Took the frontend-only option this entry names (no backend snapshot endpoint exists for `tps` — it is computed client-side from live WS token timing, so there is nothing to pull). `ACTIVE_TASK_RESTORED`'s handler now resets `chatStore.telemetry`/`snapshot` to `undefined` and `tps` to `0` in the same branch that restores the active-task header, rendering the existing neutral state (already used before any telemetry has ever arrived) instead of a frozen pre-teardown reading until the next token event happens to land.

Live-testing surfaced a tab-switch state-loss cascade (fixed in 13.0.8: an immediate-flush persist path replacing a 400ms debounce that raced `retainContextWhenHidden:false` teardown, a `mergeById` fix that stopped dropping completed local-only messages, a `WEBVIEW_READY` handshake closing a reveal-ordering race, and decoupling the WBS checklist from `AgentTimeline`'s empty-`entries` gate). One piece of the reported loss was deliberately left unfixed: `chatStore.telemetry`/`snapshot`/`tps` (the token-speedometer HUD) has no host-side mirror analogous to `_runningTasks`/`_latestPlan`, and no rehydration message is posted for it on reveal (unlike `CONTEXT_OCCUPANCY`, which already re-fetches via `APIClient.fetchContextOccupancy` on task-start/post-apply-patch). It only repopulates once fresh WS traffic (`server_telemetry`/`TOKEN_SNAPSHOT`) happens to arrive after the reveal. Nothing is destructively lost — it's a live reading, not stored data — it just reads blank/stale until the next token event. A correct fix needs either a new backend snapshot endpoint (`tps` is computed client-side only, from live WS token timing — there's no backend value to pull) or a frontend-only change to render an explicit neutral state instead of a stale number post-`ACTIVE_TASK_RESTORED`. Deferred as out of scope for the persistence-race fix.
### DEBT-197 [MEDIUM · RESOLVED 2026-08-25, 13.1.1] — `depends_on` was unenforced at dispatch; a WBS step whose dependency was rejected still ran

**Resolved:** 2026-08-25. Added exactly the one predicate this entry names, on the shared `is_dispatchable` helper (`brain/state.py`) both dispatch-selection sites already used and were documented to need moving in lockstep: it now accepts an optional `all_steps` list and, when a step declares `depends_on`, requires every named step to have reached `completed` before treating it as dispatchable. `route_to_coders` (`brain/engine.py`) and `route_after_validation`'s stall guard (`brain/guardrails.py`) both now pass the full task list. `all_steps` is optional and `depends_on` is read via `getattr` so every existing single-argument caller (including two tests using lightweight `SimpleNamespace` stand-ins for `WBSStep`) keeps compiling unchanged.

13.0.9's incremental apply gate (`brain/apply_gate.py`) made each step's terminal outcome (`completed`/`rejected`/`failed`) honest and immediate — but `route_to_coders` (`brain/state.py::is_dispatchable`) still selects purely on a step's own status, never checking `WBSStep.depends_on` against the terminal status of the step(s) it names. A step whose declared prerequisite was rejected or failed still dispatches and runs against whatever the rejected step left on disk (or didn't). `ValidateWBSDependenciesTool` only checks the graph is acyclic and every referenced `step_number` exists at plan-commit time — it has no visibility into runtime outcomes. Fix is one predicate added to `route_to_coders`'s selection (skip/fail a step whose `depends_on` entries are not all `completed`), deliberately not built in 13.0.9 to keep that batch's blast radius to the approval topology itself.
### DEBT-201 [LOW · RESOLVED 2026-08-25, 13.1.1] — `test_ssot_apply_patch_over_real_http_ws` flaked once in a full-suite run, never in isolation

**Resolved:** Added an autouse `tests/conftest.py` fixture (`_guard_litellm_patch_leakage`) that captures `litellm.aembedding`/`acompletion`'s identity before each test and forcibly restores + logs (with the offending test's `PYTEST_CURRENT_TEST` nodeid) if either differs afterward — self-healing hardening against a leaked mock corrupting a later test.


Found running the final full-suite verification for 13.0.9 (3115/3116 otherwise green). The single failure was `RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's current cancel scope` (anyio), immediately preceded in the log by a cascade of unrelated `litellm.exceptions.BadRequestError: ... model=ailienant/embedding` errors — the shape of a real, unmocked `litellm.aembedding`/`acompletion` call reaching this environment's absent provider, i.e. some OTHER test earlier in suite-collection order left a `litellm.*` patch un-restored (an autouse fixture teardown gap, not this test's own logic). Re-ran `tests/e2e/test_ssot_apply_patch_e2e.py` alone immediately after — both tests (the Auto-mode full apply and the Ask-mode interrupt/resume/apply round trip) passed cleanly, confirming the test's own logic and the underlying `apply_gate.py` feature are sound; this is suite-wide test-isolation hygiene, not a product defect. Distinct from the `vfs_manager.shutting_down` bug fixed earlier in the same session (that one reproduced deterministically across repeated runs and had an identified root cause in `main.py`; this one reproduced once in three full-suite runs and the root cause — which test leaks the litellm patch — is not yet identified). Revisit if it recurs; bisecting which test fails to restore its `litellm.*` patch would need either `pytest-randomly` bisection or an audit of every test file mocking `litellm.aembedding`/`litellm.acompletion` for a missing `monkeypatch`/context-manager scope.
### DEBT-045 [LOW · RESOLVED 2026-08-03, 12.5] — BudgetEstimatorTool uses a fixed per-action token heuristic, not a calibrated model

- **Date:** 2026-06-14 · **Resolved:** 2026-08-03 (12.5)
- **Reproduce (original):** `BudgetEstimatorTool._arun` computes `estimated_cost_usd` from static base-token constants (`write_file=1000`, `edit_file=800`, `read_file=200`, `run_command=100`) plus `len(description)//4`. These constants were chosen as conservative approximations of the cloud rate; no session-history calibration is performed.
- **Resolved:** the entry's own *Blocked by* line named the real prerequisite — `TokenLedger` (`core/token_ledger.py`) is four in-memory tier counters with no action dimension and no history, so it could never calibrate anything as-is. Built the missing side table instead: a new `action_token_usage` telemetry table (`core/telemetry.py`, mirroring `request_latency`'s bounded-window read pattern) fed by an **explicit** `action` tag threaded through the gateway's already-existing usage-recording call sites (`ainvoke`/`astream_byom`/`astream_byom_thinking`, `tools/llm_gateway.py`) — the coder's single generation call (`agents/coder.py`) tags it with `target_step.action`. `BudgetEstimatorTool` now prefers the calibrated median per action once `core.telemetry._ACTION_MIN_SAMPLES` real samples exist, else the static constant, and grades its own `confidence` (low/medium/high) by how much of the plan's step mix was calibrated — replacing the previously-hardcoded `"low"`.
- **Key decision:** an in-flight draft bridged coder→gateway via an ambient `ContextVar` — rejected in review as asynchronous global state for what is really a call attribute, and it needed a workaround for `schedule_coder_companion`'s own concurrent LLM call being miscounted against the step. Replaced with the `action` kwarg forwarded explicitly through the call chain, gated behind a `total=False` TypedDict (`_ActionKwarg`) so an untagged call never even sends the keyword — the first version sent `action=None` unconditionally and broke two hand-rolled `test_planner.py` mocks whose fixed signatures had no `action` parameter.
- **Coverage limit (declared, not a defect):** only `write_file`/`edit_file` can ever calibrate — `read_file`/`run_command` return before any LLM call, and a `response_cache` hit produces no sample either.
- **File(s):** `core/telemetry.py`, `tools/llm_gateway.py`, `agents/coder.py`, `tools/planner_tools.py`; new `tests/test_phase12_5_quality_sweep.py`.
- **Notes:** logged at 8.8.4 ship per CLAUDE.md §11.3.

### DEBT-037 [LOW · RESOLVED 2026-06-20, 8.10.9] — retrieval ablation uses mock.patch, not a production DI seam

- **Date:** 2026-06-12 · **Resolved:** 2026-06-20 (8.10.9)
- **Premise correction:** the original note attributed the `search_with_paths` patch to the G2 arm. In fact G2 (`VectorOnlyRetrievalStrategy`) patched only the graph seam; it was G1 (`ZeroShotRetrievalStrategy`) that patched the vector seams (`search_with_paths` / `search_snippets`). The substance held: both arms degraded retrieval by `mock.patch`-ing internal class methods (in `core/benchmark/strategies.py`, not `tests/`).
- **Resolved:** retrieval degradation now flows through a dependency-injection seam. The strategy objects expose `overrides()` returning callables keyed `graph_fn` / `planner_retrieval_fn` / `coder_retrieval_fn`; `arms.retrieval_overrides_for(arm)` maps each arm to its overrides; the runner folds them into `config["configurable"]`, and the planner/researcher/coder read those keys and fall back to their real bound methods when absent. Production behavior is unchanged (keys never present off-benchmark); the ablation tests assert on the override set with no `mock.patch` of retrieval internals.
- **Notes:** the routing arms (G3 `_coder_target`, G4_FORCE_CLOUD `derive_routing_decision`) are not retrieval and intentionally remain on the scoped `apply_arm` patch.

### DEBT-108 [LOW · RESOLVED 2026-08-04, 12.14] — Benchmark retention "test flake" was a real cross-thread FileLock defect

- **Date:** 2026-06-13 (first footnoted) · **Resolved:** 2026-08-04 (12.14)
- **Was:** `tests/benchmark/test_retention.py::test_run_benchmark_bounds_artifacts` had been waved through as a "pre-existing unrelated flake" across at least 6 phase-closure gates since 2026-06-13 — footnoted, never fixed. Reproduced ~1-in-3 under load, 3/3 when run alone: driving `run_benchmark` 5 times past a cap of 3 sometimes left 5 artifacts instead of 3.
- **Root cause (re-diagnosed — this was a production defect, not a test-only timing issue):** `core/benchmark_service.py::_persist_with_retention` acquired its cross-process `FileLock` inside one `asyncio.to_thread` worker (`await asyncio.to_thread(lock.acquire)`) and released it inside a *separate* `to_thread` dispatch (`await asyncio.to_thread(lock.release)`). `filelock` 3.x defaults to `thread_local=True`, so the lock's context lives in `threading.local` storage — a `release()` call on a different worker thread than the one that acquired it sees `is_locked == False` and **silently no-ops**, leaking the underlying fd. Confirmed empirically against the project's own venv (acquire on thread A / release on thread B → a fresh `FileLock` on the same path times out; same-thread acquire+release → no leak). This exactly explains the observed symptom: a leak at **run 1's** release makes every subsequent run in the same 5-run batch block the full 30s lock timeout and take the durability-first "write without prune" branch, landing at precisely 5 artifacts for a cap of 3 — and explains the load-dependence, since a quiescent default executor tends to reuse the same idle thread for both `to_thread` calls (no leak), while contention biases them onto different threads (leak).
- **Resolved:** `core/benchmark_service.py` collapsed the whole acquire → write → prune → release critical section into a **single** `asyncio.to_thread` dispatch (`_locked_write_then_prune`), so the `FileLock`'s acquire and release are structurally guaranteed to run on the same thread — the fix is provable by construction, not just empirically likely. The durability-first "write without prune on lock timeout" degrade path (and its warning log) is unchanged. The identical pattern was found in `core/memory/docs_index.py::ensure_docs_index` (acquire/release straddling a real `await _build_index()` in between, so a single-dispatch collapse wasn't available there) — fixed by passing `thread_local=False` to that `FileLock`, sharing lock state across whichever worker threads service each call. Left `gateway/ledger.py` untouched — a full-repo sweep for `to_thread(lock.acquire/release)` confirms it acquires and releases on the same thread already.
- **Tests:** the strict `== 3` cap assertion in `test_run_benchmark_bounds_artifacts` is kept, not weakened — a weaker assertion would have hidden a live lock-leak in the durability path. New `test_persist_with_retention_releases_lock_on_same_thread` reproduces the defect *reliably* rather than depending on the original ~1-in-3 load luck: it fires 16 concurrent no-op `to_thread` dispatches immediately before the benchmark run to saturate the default executor (empirically 15/15 reproduction locally against the pre-fix code, 8/8 on a second falsification pass; 5/5 passes post-fix). `iso_bench`'s fixture now also shrinks `_RETENTION_LOCK_TIMEOUT_S` to 1s so a future regression fails in ~1s instead of stalling the suite for minutes.
- **File(s):** `core/benchmark_service.py`, `core/memory/docs_index.py`, `tests/benchmark/test_retention.py`.
- **Verified:** `tests/benchmark/test_retention.py` (20 cases, including the new regression row) green in isolation and inside the full suite, repeated; full suite green; mypy 0; pyright 0.

### DEBT-150 [LOW · RESOLVED 2026-08-04, 12.14] — A hijacked interactive-PTY exec socket still leaks an `ail-docker` thread on a daemon hang

- **Date:** 2026-08-03 · **Resolved:** 2026-08-04 (12.14)
- **Was:** `_DockerPtyBackend.__init__` hijacked the exec HTTP response into a raw socket for bidirectional streaming, then blocked on `socket.recv()` with no HTTP-level timeout underneath it — the one Docker call in the module a socket-level timeout could not bound. A daemon hang mid-stream leaked the session's reader thread until the daemon eventually responded or the process restarted. Additionally, exec creation (`exec_create`/`exec_start(socket=True)`) ran inside `_PtySession.start()`'s own `asyncio.to_thread`, i.e. on the interpreter's *shared default executor* — the exact starvation vector DEBT-100 removed from every other blocking Docker call in this module, reintroduced here by construction order.
- **Resolved:** two bounds, plus a lease-leak fix found in the same code path. (1) **Interruptible reads:** `_DockerPtyBackend.read` now runs a bounded-timeout `recv_into` deadline loop (`_PTY_SOCK_POLL_S = 0.25s`, 8x headroom under `pty_session._JOIN_TIMEOUT_S`) instead of a blocking `recv`; `close()` flips a `_closed` flag before tearing down the socket, so the reader thread's next poll observes closure and exits within the join budget rather than leaking. `TimeoutError` (an idle-but-live connection) is caught **before** `OSError` — `TimeoutError` is an `OSError` subclass, and the pre-fix ordering would have silently killed a merely-idle session; `recv_into` returning `0` (real EOF) is handled separately from a poll timeout. **Portability deviation from the entry's own suggested fix:** `select`/`poll` do not work on Windows named pipes at all, and docker-py's `NpipeSocket.recv` ignores its own timeout on Windows (a plain blocking `ReadFile`) — `recv_into` was used instead, since it honours the timeout via overlapped I/O + `WaitForSingleObject` on both transports. (2) **Off the shared executor:** exec creation moved into a new `_create_pty_exec` helper dispatched through `_docker_call` onto the bounded `ail-docker` pool; `DockerSandboxAdapter.open_session` now builds the exec/socket first and passes the already-open handle into `_DockerPtyBackend`, which does no I/O in its constructor. (3) **Lease-leak fix (found in the same path):** if exec creation or `_PtySession.start()` raised, the already-acquired container lease was never released, permanently occupying a pool slot — the same failure class DEBT-152 closes on the run-lifecycle side, reached here by session-open failure instead. `open_session` now releases the lease (and closes any pre-made socket) on `BaseException` from either step, including cancellation.
- **File(s):** `core/sandbox.py` (`_create_pty_exec`, `_DockerPtyBackend`, `DockerSandboxAdapter.open_session`).
- **Verified:** `tests/test_sandbox_pool_resilience.py` (PTY2-5); full suite green; mypy 0; pyright 0.

### DEBT-151 [LOW · RESOLVED 2026-08-04, 12.14] — Sandbox pool exhaustion shares a container rather than queuing with real backpressure

- **Date:** 2026-08-03 · **Resolved:** 2026-08-04 (12.14)
- **Was:** when the container pool was at capacity with no idle lease after `SANDBOX_LEASE_WAIT_S`, a new session sharing the same mount root as an existing lease degraded to sharing that container with no fairness ordering — "whoever asks first when the wait times out wins" — and no admission ceiling, so a burst of same-project sessions could all pile onto one container.
- **Resolved:** `_ContainerPool.acquire` gained a bounded FIFO admission queue layered on the existing `asyncio.Condition` (kept rather than replaced with hand-rolled per-waiter futures — `Condition.wait_for` already handles lock release/re-acquisition and cancellation-safe re-acquisition correctly; reimplementing that by hand was the larger risk). A waiter's wake predicate requires both capacity-or-idle **and** being at the head of the queue, so releases hand off in arrival order. New knob `AILIENANT_SANDBOX_MAX_QUEUED` (default `2 * SANDBOX_MAX_CONTAINERS`) bounds the queue depth; a queue already at that depth refuses admission **immediately** (`SandboxResourceExhausted`, no wait at all) rather than letting an unbounded backlog each burn the full `SANDBOX_LEASE_WAIT_S` before degrading. The pre-existing same-mount share degrade (`_share_or_raise_locked`) is unchanged as the terminal outcome once a *queued* waiter's own wait expires — 12.6's never-crash / never-cross-mount guarantee is preserved, the queue sits in front of it. **Trade-off, documented in-code:** the queue's `notify_all()` on every release/cancellation is O(N) wake-ups (N = queued waiters, config-bounded) — every waiter wakes, evaluates its predicate, and all but the new head go back to sleep. Accepted because `SANDBOX_MAX_QUEUED` defaults small; the comment at the call site names raising that knob substantially as the trigger to migrate to a targeted per-waiter hand-off instead of broadcasting. **Fairness precedent preserved:** the existing-lease fast path (a session re-acquiring a lease it already holds) is evaluated strictly before any queue admission check, so an in-flight session's second command never queues behind unrelated new sessions' first commands.
- **File(s):** `core/sandbox.py` (`_ContainerPool.acquire`), `shared/config.py` (`SANDBOX_MAX_QUEUED`).
- **Verified:** `tests/test_sandbox_pool_resilience.py` (QUEUE1-6); full suite green; mypy 0; pyright 0.

### DEBT-153 [LOW · RESOLVED 2026-08-04, 12.14] — `response_cache` singleton has no per-test isolation, causing order-dependent cross-file cache hits

- **Date:** 2026-08-03 · **Resolved:** 2026-08-04 (12.14)
- **Was:** `core/response_cache.py`'s module-level `response_cache` singleton had no per-test reset, so two test files building an identical cache key in the same pytest process could cross-contaminate — reproduced via `pytest tests/test_coder_agent.py tests/test_tool_dispatch.py -q`.
- **Resolved:** a new autouse `_reset_response_cache` fixture in `tests/conftest.py` calls `response_cache.clear()` before every test, directly mirroring the existing `_reset_skill_embed_cache` fixture's shape and docstring discipline. No production change — `SemanticResponseCache.clear()` already existed as the documented lifecycle/test-reset hook.
- **File(s):** `tests/conftest.py`.
- **Verified:** the entry's own reproduce command (`pytest tests/test_coder_agent.py tests/test_tool_dispatch.py -q`) passes; falsified against the pre-fix conftest (fails without the fixture); full suite green; mypy 0; pyright 0.

### DEBT-033 [LOW · RESOLVED 2026-06-20, 8.10.9] — config.json ↔ MCP secret-store `key_ref` round-trip (fresh-machine import prompt)

- **Date:** 2026-06-11 · **Resolved:** 2026-06-20 (8.10.9)
- **Premise correction:** the original note described a backend wiring gap. Exploration revealed the backend was already fully shipped: `import_mcp_config` already emits a `needs_secret` list (tested in `tests/test_mcp_config_roundtrip.py`). The real gap was **frontend-only** — no MCP config-import surface existed in the extension, so `needs_secret` was never acted on.
- **Reproduce (original):** export `.ailienant/config.json` on a machine with installed credentialed servers, import on a fresh machine — server rows reconcile but there was no UI to re-prompt for missing credentials.
- **Resolved:** `ConfigImportView` added to `ailienant-extension/src/dashboard/panels/ExtensionsPanel.tsx` — native file-pick → `POST /api/v1/mcp/config/import` → credential dialog driven by `needs_secret` (cross-references the loaded registry for declared secret env-var names) → `POST /api/v1/mcp/registry/install`. Servers in `needs_secret` not present in the registry receive an informational note. Backend unchanged.
- **Notes:** Export already prevented credential leakage (userinfo redaction + no secret in JSON). This closes the usability gap end-to-end.

### DEBT-032 [LOW · RESOLVED 2026-06-20, 8.10.8] — Coder-side skill injection (planner-only shipped in 8.4.5)

- **Date:** 2026-06-11 · **Resolved:** 2026-06-20 (8.10.8)
- **Reproduce (original):** submit a task with a saved skill active — the skill directive block appeared in the planner system prompt (and therefore in the `mission_spec` the coder receives) but was **not** re-injected into the coder's own system prompt.
- **Resolved:** `agents/coder.py` now mirrors the planner seam — after the per-turn boundary UUID is minted, it reads `state.get("active_skills")` and appends `build_skill_directive_block(_skills, boundary)` to the coder system prompt (same ephemeral XML boundary as every other injected directive). No new state field — `active_skills` is already populated at task init.
- **Notes:** the planner-mediated path still shapes the whole task; the coder-side injection makes skill directives robust across multi-step coder turns.

### DEBT-027 [LOW · RESOLVED 2026-07-30, 11.13] — MCP servers testable but not auto-connected at task launch

- **Date:** 2026-06-10
- **Updated:** 2026-06-13 — Confirmed still open. `bootstrap_mcp_session` is not called anywhere in `core/task_service.py`. Comment in `api/mcp_servers.py:11` explicitly notes: "Auto-connecting saved servers at task time is a tracked follow-up." Reclassified from "Phase: 8.4.4" to Floating (8.4.4 shipped other MCP work but not this auto-connect wiring).
- **Resolved:** the 2026-06-13 update above went stale — auto-connect had in fact landed in two places (`main.py`'s `autoconnect_enabled_mcp_servers()` at host start, and a lazy first-task fallback in `core/task_service.py`), while the source comment kept asserting otherwise. 11.13 closed the one genuine remaining hole: the task-time sweep was guarded by `if not _sessions`, so *any* already-connected server suppressed it and a server added afterwards stayed dark until a host restart. The guard is gone (the sweep is idempotent per server name, so steady-state cost is one bounded DB read per task), `POST /api/v1/mcp/servers` now connects a saved enabled server immediately — best-effort, never failing the save — and `autoconnect_enabled_mcp_servers` distinguishes newly-connected from already-live so a per-task call cannot spam the log. Regression tests in `tests/test_command_menu_config.py`.
- **Reproduce:** `POST /api/v1/mcp/test` probes a server successfully, but starting a new task does not open sessions to `enabled` servers — their tools are absent from the task's `ToolRAGStore` selection.
- **File(s):** `ailienant-core/api/mcp_servers.py`; `ailienant-core/tools/mcp_adapter.py::autoconnect_enabled_mcp_servers`; `ailienant-core/core/task_service.py`.
- **Error:** coverage/wiring gap — a configured-and-enabled MCP server contributes no tools until manually bootstrapped.
- **Blocked by:** none.
- **Phase:** 11.13.
- **Notes:** cautionary case for the ledger itself — a stale "confirmed still open" note outlived the fix and was contradicted only by reading the code.

### DEBT-127 [LOW · RESOLVED 2026-08-03, 12.7] — Per-role prompt overrides are ignored by dispatched subagents

- **Date:** 2026-07-30 · **Resolved:** 2026-08-03 (12.7)
- **Reproduce (original):** save a directive override for a role in the command menu's Customize → Agents view, then have that role run as a *dispatched subagent* rather than through `run_coder_node`. The override is applied by the coder (11.13) but never reaches the subagent.
- **Resolved:** `agents/roles.py` gained a dedicated `build_subagent_system_prompt(role, override)` seam — the "dedicated seam rather than reusing the coder builder verbatim" this entry's own note anticipated. It composes the role directive (or the saved override) plus `LANGUAGE_MIRROR_DIRECTIVE`, deliberately *without* `_BASE_CODER_PROMPT` (whose SEARCH/REPLACE contract does not apply to a subagent constrained to a `response_schema`-driven JSON answer). `subagent_worker` resolves it once from `state["agent_role_overrides"]` (the same channel `agents/coder.py` reads) and threads it into both the tool-loop seed message and the final-answer synthesiser — the latter via a `_make_default_answer(system_prompt) -> AnswerFn` closure that keeps the `AnswerFn` signature, and therefore the `dispatch_answer_fn` test seam, unchanged. A role absent from `ROLE_REGISTRY` (e.g. `analyst_readonly`, which has no directive/override concept of its own) falls back to the language-mirror directive alone rather than inheriting `get_role_config`'s `core_dev` default.
- **Notes:** unblocked by DEBT-106 landing in the same slice, as this entry anticipated.

### DEBT-128 [LOW · RESOLVED 2026-08-04, 12.8] — `analyst_name` setting is persisted but never read

- **Date:** 2026-07-30 · **Resolved:** 2026-08-04 (12.8)
- **Was:** the Analyst name round-tripped through `~/.ailienant/settings.json` and the dashboard, but the persona the agent actually adopts was unchanged — `brain/personality.py`/`shared/persona.py` read SOUL.md only.
- **Resolved:** new `api/system_settings.py::resolve_analyst_name()` (fault-tolerant, defaults `"Natt"`) feeds two call sites: `SoulManager.get_prompt()` appends a name clause AFTER `compose()`'s ADR-701 identity clause (never before it — a form of address, not an identity override) at every one of its four return points; `core/task_service.py::_resolve_chat_system_prompt` gained a parallel `_resolve_analyst_name_directive()` alongside the existing `_resolve_output_style_directive()`. Default name contributes nothing (byte-identical prompt), preserving `test_prompt_prefix_stability.py`.
- **File(s):** `ailienant-core/api/system_settings.py`, `ailienant-core/brain/personality.py`, `ailienant-core/core/task_service.py`.

### DEBT-129 [MEDIUM · RESOLVED 2026-08-03, 12.7] — Coder registry-fallback tools have no interactive HITL approval channel

- **Date:** 2026-07-30 · **Resolved:** 2026-08-03 (12.7)
- **Reproduce (original):** drive `run_agentic_cell_node` with a reasoner that proposes a tool name outside the 3 `CELL_TOOLS` primitives, resolving to an EXECUTE/WRITE-tier tool under a session mode that would normally trigger HITL (e.g. DEFAULT). The `ToolDispatcher.dispatch()` call denies with "requires human approval, but no approval channel is available" instead of raising the interactive approval card.
- **Resolved:** generalized the interrupt exactly as this entry anticipated, without wiring `make_websocket_approval_fn` into the memoized fallback dispatcher (its mid-loop-interrupt replay-safety warning still holds). `core/tool_dispatch.py::ToolDispatcher` gained a pure `classify(call) -> (Optional[RegisteredTool], PermissionDecision, Optional[str])` — the single gate implementation `dispatch()` now also calls internally, so there is exactly one place lookup-miss/role/tier decisions are made. The cell's fallback branch calls `classify()` *before* `dispatch()`; on `HITL` it stops processing further calls this iteration, commits any edits already computed, sets a new `pending_tool_call: Optional[Dict[str, Any]]` state channel (`{"name", "args"}`, additive/last-value — `SCHEMA_EVOLUTION.MD §48`), and returns `status="continue"`. A new tool-call-approval phase at the top of the node resumes `interrupt()`-first (mirroring `pending_exec_command`'s own phase), resolves the approved tool by **exact name** against `tool_rag_store.all_schemas()` — never by re-running the intent-ranked `select_tools()`, whose ranking is not stable across a suspend/resume boundary — and dispatches it exactly once through a fresh, single-tool dispatcher with a pre-approved `approval_fn`. Trust-once is deliberately not applied, matching the exec-approval phase. New security flag `TOOL_CALL_HITL_DENIED` on operator denial.
- **Notes:** the 3 primitives' own `pending_exec_command` defer path is untouched; this is the additive sibling for the registry-fallback branch specifically.

### DEBT-130 [MEDIUM · RESOLVED 2026-08-03, 12.7] — Coder's one-shot path (`run_coder_node`) still has no tool-calling

- **Date:** 2026-07-30 · **Resolved:** 2026-08-03 (12.7)
- **Reproduce (original):** any WBS step the planner does NOT flag as `requires_iteration` routes to `agents/coder.py::run_coder_node`'s single-shot SEARCH/REPLACE call — no `ToolDispatcher`, no `core/tool_registry.py` resolution, same as before Division 8.18.
- **Resolved:** a bounded READ_ONLY tool-grounding pre-pass now runs *between* context assembly and generation — a separate reasoning call through the identical `select_tools`/`resolve_tools`/`ToolDispatcher` substrate, never the SEARCH/REPLACE call itself (that output contract stays strict and never scaffolded). Tier-ceilinged to READ_ONLY by design, not merely by convention: DEBT-068's ruling that mutation stays the agentic cell's surface holds, and `run_coder_node` is re-entered by the `error_correction` retry loop, so a mutating call here would violate the idempotency invariant (§5.3) — a READ_ONLY pass needs no approval channel and stays idempotent under retry. Two gates keep it cheap on the trivial majority: `_needs_grounding` fires only when the step is thin on context already (new file / empty RAG / retry-after-validation-feedback), and `_grounding_admitted` skips entirely under `ASK_ALL` (the one mode where READ_ONLY resolves to HITL, and this pre-pass wires no approval channel) rather than paying a round-trip for a guaranteed denial. Observations fold into the coder's existing L5 execution context (trimmable under budget pressure, same as the RAG/style blocks) and into the response-cache key.
- **Notes:** DEBT-129/106 were the two capability gaps genuinely blocking a mutating tool-loop on this path; the READ_ONLY scope sidesteps both rather than waiting on them.

### DEBT-140 [MEDIUM · RESOLVED 2026-08-03, 12.13] — GraphRAG had no chunking; one vector per whole file

- **Date:** 2026-08-03 · **Resolved:** 2026-08-03 (12.13)
- **Detail (was):** `core/memory/semantic_memory.py::semantic_upsert` embedded an entire file's
  content as a single LanceDB vector, regardless of size. A large multi-function file collapsed to
  one centroid that resembled none of its individual functions — retrieval degraded to "which file
  is vaguely near this topic" rather than "which function actually matches". Independent of which
  embedding model was configured; a larger/costlier model diluted identically.
- **Resolution:** hybrid-by-size chunking, additive throughout. Files under `_CHUNK_FILE_MIN_TOKENS`
  (800) keep the unchanged single file-level vector; files over it additionally emit one vector per
  `function`/`method` symbol (classes excluded — their range fully contains their methods') into a
  new table, `symbol_chunk_embeddings`, sourced from `IndexingResult.symbols` — already produced by
  the single existing tree-sitter parse at both indexing call sites, zero re-parses. `search_snippets`
  merges both tables under one `asyncio.gather` (latency `max(file, chunk)`, never the sum) and packs
  multi-hit evidence per file under a nearest-first greedy budget
  (`_MAX_EVIDENCE_CHARS_PER_FILE = 2000`); the routing meters (`search`, `search_with_paths`, and
  therefore CSS/`is_red_alert`) read only the file table, proven byte-identical with and without
  chunk rows present. `semantic_delete`, the dimension-mismatch drop/recreate path, and
  `core/janitor.py::_vector_gc_sync` all cover both tables. A new `POST
  /api/v1/memory/chunks/backfill` endpoint (deny-if-busy per project, bounded, resumable, `confirm`
  gated) adopts a corpus indexed before chunking existed — see `SCHEMA_EVOLUTION.MD §46`.
- **Corrections to the original spec, found before implementation:**
  1. The spec said to source symbols from the `symbol_definitions` catalog — that catalog is
     populated only by the *reactive* (per-save) path, never by the cold/bulk indexer, so a
     freshly-indexed workspace would chunk nothing. Fixed by sourcing from `IndexingResult.symbols`
     instead, available at both call sites already.
  2. The spec said landing chunks would let query-time skeleton distillation (DEBT-142) be deleted
     outright. It cannot: hybrid-by-size means under-threshold files never get chunk rows, so
     deletion would regress every small/medium file back to the raw 500-char slice. It stays as the
     fallback tier for files with no stored chunk evidence, with both its containment layers intact.
  3. `pca_project_2d` (cited as needing an update) is a pure function with no table coupling — no
     change needed. Surfacing chunks in the dashboard scatter map is a separate frontend feature,
     logged as DEBT-148.
  4. "Adoption requires a full reindex" named a procedure that does not exist in this codebase (no
     CLI, no endpoint, no UI command; `LazyIndexer` actively skips its crawl once a workspace is
     already indexed) — replaced with the bounded backfill endpoint above.
  5. Without content-addressed reuse, the write path would have *inverted* the recorded cost result:
     a blanket per-save replace-write of a file's chunk rows means editing one function re-embeds
     every symbol in the file, not just that one. Fixed with a `content_hash` (sha256 of the chunk's
     own text) reuse key — a chunk whose text is unchanged reuses its stored vector regardless of
     line-number movement above it. The key is deliberately the text hash, not
     `(qualified_name, start_line)`: line numbers shift on any edit above a symbol, and a positional
     key would falsely mark every chunk dirty on a one-line insert, re-embedding the whole file.
  6. Reusing the file-level `create_index(replace=True)` pattern per file would make the backfill
     endpoint rebuild the ANN index once per file — quadratic over a large corpus. `_write_chunks`
     takes a `build_index` flag; backfill defers to a single build after its whole batch.
  7. Batched embedding requests need internal partitioning by BOTH item count (32) and cumulative
     token payload, not count alone — a handful of large functions can breach a provider's payload
     ceiling well under the 32-item bound, surfacing as an HTTP 413 or a silently truncated response
     array on OpenAI-compatible local providers (Ollama/vLLM/LM Studio).
- **Corrected cost result:** the file-level vector is still re-embedded on every save, exactly as
  before — content-addressed reuse does not change that. What it buys is that the *added* chunk cost
  scales with the number of symbols actually edited (typically one), not with the file's symbol
  count: steady-state incremental cost is `1 + M` (M = edited symbols) against the pre-chunking `1`.
  The genuine one-time cost is the initial index or backfill pass of over-threshold files.
- **Notes:** `_CHUNK_MIN_TOKENS = 20` is the chunk-scoped anti-fragmentation floor — `_MIN_TOKENS`
  (100) is calibrated for whole files and would drop nearly every real function if reused directly.
  A module-level assertion enforces `_CHUNK_FILE_MIN_TOKENS > _MIN_TOKENS` so a file can never
  qualify for chunking without first qualifying for its own file-level embedding.
- **Tests:** `tests/test_symbol_chunk_embeddings.py` (23 cases — hybrid gate, class/empty-slice
  exclusion, failure isolation, batched-embedding partitioning and fallback, the three-case
  content-addressed reuse guard, gather-based merge and degrade-on-exception, the evidence knapsack,
  additive tolerance with no chunk table, routing-meter isolation, both-table delete/GC/dimension
  coverage, and backfill idempotency/limits/single index build).

### DEBT-141 [MEDIUM · RESOLVED 2026-08-03, 12.11] — Silent embed-input truncation, fixed-constant ceiling

- **Date:** 2026-08-03 · **Resolved:** 2026-08-03 (12.11)
- **Detail (was):** `semantic_upsert` truncated content exceeding `_MAX_EMBED_TOKENS` (a fixed 8191
  module constant) via a tiktoken round-trip with no log line — the dropped tail became permanently
  invisible to vector search with zero trace, and the stored `token_count` recorded the *pre-truncation*
  value, so an already-indexed file could not even be audited for it after the fact. The ceiling also
  ignored the active embedding provider entirely.
- **Resolution:** `core/config/byom_config.py::EmbeddingTarget` gained an additive
  `max_input_tokens: int = 8191` field (every existing keyword-based construction site and persisted
  `byom_config.json` keeps working unchanged). `semantic_upsert` now resolves its ceiling from
  `get_embedding_target().max_input_tokens` and emits `logger.warning` on truncation carrying the
  path, real token count, applied ceiling, and tokens dropped. `cl100k_base` remains the measurement
  tokenizer (a deliberate conservative proxy — a per-provider tokenizer would add a dependency per
  provider for a narrow accuracy gain, CLAUDE.md §9), now documented as such rather than assumed
  silently. No `truncated` column was added to the LanceDB row (would force a table recreate/full
  reindex for an observability-only win) — the warning makes truncation auditable going forward;
  retroactive auditing of rows indexed before this fix stays impossible.
- **Tests:** `tests/test_graphrag_retrieval_fidelity.py` (3 cases: warning fires with real counts,
  ceiling resolves from the active target not the fixed default, no warning when under budget).

### DEBT-142 [MEDIUM · RESOLVED 2026-08-03, 12.11] — RAG evidence was a 500-char head-of-file slice

- **Date:** 2026-08-03 · **Resolved:** 2026-08-03 (12.11)
- **Detail (was):** `workspace_embeddings.content_snippet` is documented "first 500 chars for
  audit/debug" but `search_snippets` returned it verbatim as retrieval *evidence* to four production
  consumers — `agents/coder.py::_fetch_rag_snippets`, `core/task_service.py`'s `_build_rag_context`
  and `_rag_snippets` (chat + analyst), and the MCP `query_memory` tool (`gateway/handlers.py`, an
  external wire contract). A file that matched the query on line 400 contributed only its import
  header — the evidence shown was disconnected from the reason the file matched.
- **Resolution:** `search_snippets` gained an additive `project_root: Optional[str] = None` param;
  when supplied, each stored hit is distilled at query time into a whole-file AST skeleton via the
  existing `core/vfs_middleware.py::make_safe_reader` + `core/ast_engine.py::extract_skeleton` (both
  pre-existing, the latter already used by `agents/coder.py::_build_style_block`). All four
  production call sites now thread their `workspace_root`/`project_root` through
  (`agents/coder.py::_fetch_rag_snippets` gained a `workspace_root` param; `task_service.py`'s
  `_build_rag_context`/`_stream_chat_answer`/`_rag_snippets` do the same; `gateway/handlers.py`
  passes `args["workspace_root"]` — an MCP-visible evidence-quality change, noted in
  `SCHEMA_EVOLUTION.MD §45`). Return type is unchanged (`List[Tuple[str, str]]`), so
  `filter_relevant_snippets`, `_build_rag_block`, and `_build_style_block` needed no change.
  Omitting `project_root` degrades gracefully to the pre-fix `content_snippet` for every result.
- **Containment (accepted as a temporary query-time tradeoff, not the end state):** this re-parses
  the matched file on every retrieval (O(K·N) in top-K and AST size) on the hot path. Layered
  defense against a pathological input hanging the shared thread pool: (1) a `_DISTILL_MAX_CHARS`
  size guard skips distillation before the parser ever sees an oversized file — the primary
  protection; (2) `asyncio.wait_for(..., timeout=_DISTILL_TIMEOUT_S)` bounds the caller's wait.
  Residual, documented risk: `wait_for` frees the awaiting coroutine but cannot kill the underlying
  thread — a stalled parse keeps running in the shared default `ThreadPoolExecutor` (also used by
  LanceDB calls) until it finishes regardless. The size guard is what actually protects that shared
  pool; the timeout only bounds latency. **Update (12.13):** index-time chunk embeddings
  (DEBT-140) narrowed this path's exposure — files over `_CHUNK_FILE_MIN_TOKENS` carry stored
  per-symbol evidence and never reach this distillation at all — but did not remove it or its
  containment layers, as originally expected here. Hybrid-by-size chunking means under-threshold
  files still have no stored evidence, so this stays the load-bearing fallback tier for them; see
  DEBT-140's corrections for why the removal this note anticipated turned out to be wrong.
- **Tests:** `tests/test_graphrag_retrieval_fidelity.py` (skeleton returned instead of a head-slice
  when the match is past line 500; graceful fallback with no `project_root`, oversized content, and
  a stalled parse; `agents/coder.py` and the MCP handler both forward `project_root` correctly).

### DEBT-143 [MEDIUM · RESOLVED 2026-08-03, 12.11] — `deep_parse` uncapped; capped sibling `extract()` was dead code

- **Date:** 2026-08-03 · **Resolved:** 2026-08-03 (12.11)
- **Detail (was):** `core/memory/graphrag_extractor.py::deep_parse` — the only retrieval-expansion
  path with a live caller (`agents/researcher.py`) — VFS-read and Tree-sitter-parsed *every* 1-degree
  neighbor of its seed files with no cap, violating the CLAUDE.md §5.5 defensive-pagination
  invariant. Meanwhile `GraphRAGDynamicExtractor.extract()` — a PPR-ranked, tier-budgeted sibling
  method with file-count and token-ceiling guardrails — had zero callers in production *and* in
  tests, and its guardrail (`_apply_guardrails`) counted tokens of the file *path*, not its content,
  even where it was reachable.
- **Resolution:** merged the guardrail logic into `deep_parse` (the live path) rather than wiring the
  dead `extract()`. Expanded neighbors are ranked by PPR (`_fetch_ppr_scores`, reused as-is) before
  the cap; seeds always come first and keep the caller's own order, since they carry actual
  vector-relevance to the query — PPR is query-blind centrality and must never override that signal,
  only break ties among neighbors that have none. `_deep_parse_sync` now stops once
  `_MAX_FILES[_DEFAULT_ROUTING]` (10) files are parsed or the next file's block would push the
  running `context_block` past `_TOKEN_CEILING[_DEFAULT_ROUTING]` (4096) — both measured against
  real, incrementally-tiktoken-encoded content, never a path-length proxy. `deep_parse` has no
  routing-tier signal from its caller, so it always budgets against `LOCAL_SMALL`, the same
  conservative default the rest of the codebase falls back to when no tier is known
  (`agents/planner.py`, `agents/researcher.py`). `extract()`, `ExtractionResult`, and
  `_apply_guardrails` were deleted (confirmed zero callers before removal); `_bfs_k_hop` and the
  public `bfs_k_hop_forward`/`bfs_k_hop_backward` wrappers were kept — those are live via
  `tools/perception_tools.py`'s blast-radius tools and are a structurally separate, caller-owned-depth
  surface from `deep_parse`'s fixed 1-degree expansion.
- **Correctness note (caught in review before landing):** the cap is applied to the *read/parse
  loop*, never to the `target_files` list itself — `DeepParseResult.target_files` stays the full,
  pre-cap neighbor set, and the new `truncated: bool` field plus `coverage_ratio` (still
  `len(parsed_files) / len(target_files)`) are computed against that same pre-cap denominator. An
  earlier draft of this fix would have shrunk `target_files` before capping, which inflates
  `coverage_ratio` — that metric flows into `graph_coverage` at 0.3 weight of CSS and gates
  `is_red_alert` (`agents/researcher.py`), so the bug would have made the system report *better*
  context health exactly when it truncated context. Caught and fixed before implementation, not
  after.
- **Tests:** `tests/test_graphrag_retrieval_fidelity.py` (seeds-first/PPR-ranked-neighbors ordering;
  file-count cap with the coverage-inflation regression explicitly asserted; token-ceiling cap
  binding independently of file count; seed-only/no-neighbors and empty-seeds edge cases;
  `extract()`/`ExtractionResult`/`_apply_guardrails` confirmed removed via `hasattr`).

### DEBT-144 [MEDIUM · RESOLVED 2026-08-03, 12.12] — `brain/prompt_builder.py` is fully dead code

- **Date:** 2026-08-03 · **Resolved:** 2026-08-03 (12.12)
- **Detail (was):** `PromptBuilder.build_context` and the module-level `build_system_prompt` — a
  ~330-line, token-budget-aware context assembler with flesh/skeleton tiering and a real
  `PrecisionTokenCounter`-driven budget — had zero references anywhere outside
  `brain/prompt_builder.py` (verified: no callers of `PromptBuilder`, `ContextBundle`,
  `build_context`, or `build_system_prompt`). Its file selection was global-PPR
  (`core/db.py::get_top_ppr_files`) — "the project's most central files" — which is query-blind and
  would have injected the same files into every turn regardless of the actual question.
- **Resolution:** deleted `brain/prompt_builder.py` outright, not wired in — its two genuinely
  valuable mechanics (real token-budget accounting, flesh/skeleton tiering) were already harvested
  into the query-relevant retrieval path by DEBT-142 (12.11), the query-aware surface where that
  discipline actually helps; `build_system_prompt` was separately superseded by 12.1's
  `build_static_identity_prompt`/`build_boundary_declaration` split in `agents/prompts.py`. The
  deletion cascaded two levels further than the entry originally described: `brain/orchestrator.py`
  (37 lines) had exactly one caller repo-wide — `prompt_builder.py:193`'s
  `get_partial_context_prefix()` call — so it was dead the moment its only caller was; and
  `LazyIndexer.progress_percentage` (`core/indexer.py`) had exactly one consumer —
  `orchestrator.py:29` — so it followed. Verified before deletion that the live IDE progress bar does
  not depend on that property: `api/websocket_manager.py::broadcast_indexing_progress` computes its
  own percentage locally from the `(current, total)` args `core/indexer.py` already passes it: the
  property was a dead duplicate of that formula, not its source. `agents/orchestrator.py` — a
  same-basename, unrelated, and very live module (`run_orchestrator_node`, consumed by
  `brain/swarms.py` and tested by `tests/test_orchestrator.py`) — was deliberately not touched.
  Retargeted the one hard-constraint test
  (`tests/test_phase7_13_checkpoint_gate.py::test_dd1_single_vfs_reader_and_named_retries`, which read
  `brain/prompt_builder.py` as raw text) by removing its two now-unresolvable assertion lines; the
  rest of the DD1 invariant (three `agents/` files + two retry constants) is untouched. Scrubbed three
  now-stale basename-collision comments that cited the deleted `brain.orchestrator` as their worked
  example (`mypy.ini`, `agents/__init__.py`, `brain/__init__.py` — `brain/` now participates in no
  basename collision at all) and two doc-comments naming the deleted module
  (`core/deferred_tool_loader.py`, `core/tool_rag.py`). `DEVELOPERS.md` needed no change — its
  Repository Layout never listed either deleted module. The orphaned `is_indexing_complete` state
  channel this cascade also surfaced was deliberately left alone (removing a
  `AIlienantGraphState` field is a checkpoint-contract change, out of scope for a cleanup pass) and
  logged separately as DEBT-146.
- **Tests:** the three explicitly re-run — `tests/test_phase7_13_checkpoint_gate.py` (the edited
  gate), `tests/test_orchestrator.py` (the surviving, unrelated `agents/orchestrator.py`),
  `tests/test_indexer_warmup.py` (the surviving `LazyIndexer.is_complete`) — plus the full suite, all
  green. A zero-reference sweep for `prompt_builder|PromptBuilder|ContextBundle|build_system_prompt|
  brain.orchestrator|orchestrator_context|progress_percentage` returns nothing outside history/docs.

### DEBT-146 [LOW · RESOLVED 2026-08-25, 13.1.1] — `is_indexing_complete` graph-state channel was write-only

- **Resolved:** 2026-08-25. Re-verified the claim: `is_indexing_complete` was still declared on `AIlienantGraphState`, still hardcoded `True` at its one production write site (`core/task_service.py`), and still read nowhere repo-wide (exhaustive grep, zero readers). The §10 concern this entry itself raised (removing a persisted-checkpoint `TypedDict` field is a contract change) was re-examined: Python's dict-based checkpoint state tolerates an unknown/extra key on load without error, so an old checkpoint carrying this field poses no resume-time risk. Deleted the field from `brain/state.py`, its write site in `core/task_service.py`, and the matching test fixture in `tests/test_micro_swarm_e2e.py`; annotated (not deleted) the corresponding row in `docs/SCHEMA_EVOLUTION.MD` per that document's own "fields are never deleted, deprecated fields carry a `Deprecated:` annotation" convention.

- **Date:** 2026-08-03
- **Detail:** `AIlienantGraphState.is_indexing_complete` (`brain/state.py`) is declared and seeded
  hardcoded `True` at exactly two sites (`core/task_service.py`, `tests/test_micro_swarm_e2e.py`) —
  a strict repo-wide search finds zero readers. Surfaced while tracing the DEBT-144/12.12 deletion
  cascade: it is the other half of the same never-completed "workspace indexing gate" idea that
  `brain/orchestrator.py`'s (now-deleted) `get_partial_context_prefix()` belonged to — that half
  warned the model in-prompt when indexing was incomplete; this half was presumably meant to gate
  something structurally, but nothing ever read it.
- **Phase:** future state-cleanup slice. Deliberately not fixed in 12.12: removing a field from the
  `AIlienantGraphState` `TypedDict` is a persisted-checkpoint-contract change (CLAUDE.md §10,
  additive-only), not a same-pass cleanup item — a checkpoint written before removal would carry the
  now-unknown key, and every reader must keep tolerating it either way. The live system already
  surfaces "context is thin" through CSS / `is_red_alert` / `is_corpus_empty`, so this channel's
  original purpose is not a coverage gap, just an unused wire.
### DEBT-132 [LOW · RESOLVED 2026-08-04, 12.8] — Background-task executions get no Glass-Box Timeline I/O detail

- **Date:** 2026-07-30 · **Resolved:** 2026-08-04 (12.8)
- **Was:** `BackgroundTaskManager.create`'s own `asyncio.create_subprocess_shell` path bypassed `core/exec_log.py::record_execution` entirely — a spawned task never appeared with an expandable execution box.
- **Resolved:** rather than the speculated new `SandboxAdapter` ABC background-execution method (a bigger change for one caller), `create`/`_watch` emit directly to the turn's `ActivitySink`: `create` opens the span with `task_id` doubling as the correlation ref (already a uuid4 hex, already the registry key); `_watch` resolves it with a masked/capped terminal detail on every exit path, including the `cancelled` race-guard branch (which previously returned silently, leaving nothing to close the row). `_watch` is a task spawned from `create`, so it inherits the ambient sink contextvar for free.
- **Declared tradeoff:** a background task can outlive the turn that spawned it, so its detail may land after the timeline has already collapsed to its summary — honest for a background task (that's what "background" means), not a defect.
- **File(s):** `ailienant-core/tools/execution_tools.py`.

### DEBT-133 [LOW · RESOLVED for tool calls 2026-08-04, 12.8] — File-read and MCP-tool-call I/O not on the Glass-Box Timeline

- **Date:** 2026-07-30 · **Resolved (tool calls):** 2026-08-04 (12.8)
- **Was:** a `read`-kind marker (a file read) or an MCP/registry tool call rendered as a plain one-line marker — no expandable body.
- **Resolved (tool calls):** `core/tool_dispatch.py::ToolDispatcher.dispatch` now instruments the `ActivitySink` directly — the single chokepoint all three live consumers (the agentic-cell fallback, the coder's READ_ONLY grounding pre-pass, dispatched subagents) share since 12.7. A denied/unresolved/undispatched call emits a ref-less `emit_blocked`; an executed call gets marker + masked/capped detail (args + observation, via a new shared `core/redaction.py::truncate_middle`). A HITL-tier fallback call (DEBT-129's `pending_tool_call` defer) opens its span BEFORE the approval round-trip and carries a stable `activity_ref` through the checkpointed channel so a LangGraph replay of the resume phase never opens a duplicate row — see `docs/SCHEMA_EVOLUTION.MD` §50.
- **Resolved (file-read, narrower than the original ask):** rather than instrumenting `make_safe_reader` (sync, no natural await point, and used by many non-agent-driven callers), the coder's own single `reading {file}` marker gained a size metric, computed after the read completes. A tool-initiated read (the `read_file` registry tool) is already covered by the tool-call detail above — no double instrumentation.
- **Deliberately NOT done:** a masked content preview of the file itself. Re-logged as **DEBT-155** — file content is unbounded and sensitive in a way command stdout is not (source code vs. arbitrary shell output), and needs its own truncation/redaction design pass, not a copy of `record_exec`'s masking.
- **File(s):** `ailienant-core/core/tool_dispatch.py`, `ailienant-core/core/redaction.py`, `ailienant-core/agents/coder.py`, `ailienant-core/brain/agentic_cell.py`.

### DEBT-134 [LOW · RESOLVED 2026-08-04, 12.8] — Execution-detail output fills on completion, not incrementally

- **Date:** 2026-07-30 · **Resolved:** 2026-08-04 (12.8)
- **Was:** the timeline node's I/O box stayed empty until a command finished, then filled all at once — no line-by-line streaming.
- **Resolved:** rather than rebuilding one-shot EXECUTE-tier tools onto `SandboxSession` (the originally-speculated, much larger fix), the devcontainer tier — the one transport that already streams host→backend incrementally, since 12.4/DEBT-083 — now forwards those already-arriving chunks live. `core/activity_context.py` gained a second `ContextVar` (`bind_exec_ref`/`current_exec_ref`), bound by `record_execution` only around its `adapter.execute()` await; `api/devcontainer_bridge.py::WebSocketHostBridge.exec_command` reads it (paired with the sink) to register its own transport-level `request_id` against the timeline's `exec_id`. A new `server_activity_detail_chunk` WS event carries each masked fragment. Bounded on both sides (charter §5.5): a backend cumulative-character cap (`_LIVE_STREAM_CAP`, 16,000 chars) stops forwarding and sends one suppression notice past the ceiling; a frontend retention clamp (`MAX_LIVE_EXEC_FIELD_CHARS`, 4,000 chars, head+tail preserved) bounds the store regardless. The terminal detail always REPLACES accumulated chunk text, never appends to it — a stray late chunk after settle is a no-op. Docker/Wasm/native-host tiers are unaffected, still filling on completion.
- **File(s):** `ailienant-core/core/activity_context.py`, `ailienant-core/core/exec_log.py`, `ailienant-core/api/devcontainer_bridge.py`, `ailienant-core/api/websocket_manager.py`, `ailienant-core/api/ws_contracts.py`, `ailienant-core/core/task_service.py`; `ailienant-extension/src/api/contracts.ts`, `ailienant-extension/src/workspace/utils/timelineBuilder.ts`, `ailienant-extension/src/workspace/hooks/useWSMessageHandler.ts`, `ailienant-extension/src/shared/config.ts`.

### DEBT-161 [LOW · Floating] — 471 phase/ADR references survive in production code (§13.1/§13.2)

- **Date:** 2026-08-06
- **Reproduce:** `grep -rEn '(Phase|Division)\s+\d+(\.\d+)*|ADR-\d+' ailienant-core --include=*.py` plus
  the equivalent sweep over `ailienant-extension/src/**/*.{ts,tsx}`, excluding `tests/`/`test/`/`e2e/`.
  Found while auditing the 12.10 gate — no prior Phase 8-12 sub-phase closure ever measured this.
- **File(s):** 130 production files, 471 lines total. Top offenders: `brain/state.py` (39),
  `ailienant-extension/src/providers/workspace_panel.ts` (35), `core/sandbox.py` (30),
  `core/task_service.py` (29), `src/api/api_client.ts` (14), `tools/mutation_tools.py` (13),
  `src/shared/config.ts` (12), `core/db.py` (11), `agents/mcts_coder.py` (10),
  `src/workspace/workspaceStore.ts` (9), `src/workspace/components/PromptBar.tsx` (9), `main.py` (8).
- **Error:** CLAUDE.md §13.1 requires active scrubbing of phase/sub-phase/ADR/blueprint references on
  touch; §13.2 forbids new ones. Neither is retroactive — the violation is historical accumulation
  across every phase before this charter version existed, not a regression from any single change.
- **Blocked by:** nothing technical. Explicitly deferred rather than swept in the 12.10 gate itself:
  §13.1 is a Boy-Scout ("when you encounter") rule, a checkpoint gate is test-only by the project's own
  sibling convention, and 471 edits across 130 files immediately before a Phase 13 launch is exactly
  the uncontrolled blast radius a gate exists to avoid introducing.
- **Phase:** future phase-reference scrub slice — file-by-file, verified against `git blame` context so
  a reference that also documents a still-relevant migration window (e.g. a legacy-alias sunset note)
  is rewritten to be timeless rather than deleted outright.
- **Notes:** the 12.10 gate's `LANG1` regression row is scoped to Spanish only (DEBT-not-needed, closed
  outright in the same pass); this entry is the phase-reference class only, deliberately not conflated.


### DEBT-162 [LOW · RESOLVED 2026-08-25, 13.1.1] — Three REST contract models in `api/api_contracts.py` were dead code

- **Resolved:** 2026-08-25. Re-verified: `TaskSubmitRequest`/`TaskSubmitResponse`/`IDEContext` still had zero references outside their own module, and the live task-submit path (`main.py:771`, `POST /api/v1/task/submit`) confirmed to use an entirely different model (`TaskPayload` from `core/task_service.py`), not these. Deleted all three; `DirtyBuffer` (the fourth class in the same file) is genuinely live — `main.py` imports and constructs it — and was left untouched. The now-unused `ManualAttachment`/`Optional` imports were dropped alongside.

- **Date:** 2026-08-06
- **Reproduce:** `grep -rn 'TaskSubmitRequest\|TaskSubmitResponse\|IDEContext' ailienant-core --include=*.py`
  outside `api/api_contracts.py` itself returns nothing — the live task-submission path is
  WebSocket-based (`core/task_service.py`), not a `POST /task/submit` REST endpoint these models imply.
- **File(s):** `ailienant-core/api/api_contracts.py`.
- **Error:** same shape as DEBT-144 (closed by 12.12's `brain/prompt_builder.py` reclamation) — fully
  dead capability, never called at runtime, that documentation and future readers could mistake for
  the live contract.
- **Blocked by:** nothing. Not resolved in the same pass this entry was logged: deleting a model
  used by `DirtyBuffer`'s sibling classes needs a one-name-at-a-time check that no other in-flight
  branch or external MCP client references the REST shape, which is beyond a documentation-accuracy
  pass's blast radius.
- **Phase:** future dead-contract reclamation slice, same pattern as 12.12 — verify zero references,
  delete, scrub the two doc-comments this cascade would surface if any exist.
- **Notes:** the file's own header comment (`# alienant-core/core/api_contracts.py`) was also wrong
  (wrong directory, misspelled project name) — fixed in the same pass that discovered this entry
  (12.10), since it was a one-line Boy-Scout fix, not a scope-widening deletion.
### DEBT-163 [MEDIUM · RESOLVED 2026-08-25, 13.1.1] — `resolve_default_adapter()`'s `docker.from_env()` call had no timeout

- **Resolved:** 2026-08-25. **Correction to this entry's own claim:** it names line 2399 (`pull_sandbox_image`) as already using the shared `_docker_call` helper with an explicit `timeout_s` — verified against the pre-fix source (`git show HEAD:...`) that this was false; `pull_sandbox_image` called `await asyncio.to_thread(docker.from_env)` bare, with no timeout and no breaker, a second live instance of the same gap. Fixed both: `resolve_default_adapter`'s Tier-1 probe now routes `docker.from_env` through `_docker_call` at `_DOCKER_PROBE_TIMEOUT_S`; `pull_sandbox_image` routes it through `_docker_call` at `DOCKER_OP_TIMEOUT_S`. Both now get the same bounded-thread + timeout + breaker treatment every other `docker.from_env` call site in the file already had.

- **Date:** 2026-08-12
- **Reproduce:** `grep -n "docker.from_env" ailienant-core/core/sandbox.py` — line 2204
  (`resolve_default_adapter`) calls it bare and synchronous; every other call site in the same file
  (lines 344, 991, 1003, 1241, 2399) wraps it in `asyncio.to_thread(docker.from_env, ...)` with an
  explicit `timeout_s=DOCKER_OP_TIMEOUT_S` via the shared `_docker_call` helper.
- **File(s):** `ailienant-core/core/sandbox.py:2192-2214` (`resolve_default_adapter`).
- **Error:** CLAUDE.md §5.1 requires an explicit timeout on every external call in the
  Gateway/Transport zone. `docker.from_env()` constructs the low-level Docker client — depending on
  docker-py version and the host's Docker context config, this constructor can itself make a network
  call (version negotiation / TLS handshake) with no bound. Only the subsequent `client.ping()` is
  wrapped in `asyncio.wait_for(..., timeout=_DOCKER_PROBE_TIMEOUT_S)` (line 2205-2207) — a
  misconfigured or slow Docker context could make `resolve_default_adapter` hang indefinitely on
  startup before ever reaching the bounded ping.
- **Blocked by:** nothing. Found investigating the nightly e2e CI timeout (nothing else in
  `lifespan()` was unbounded once `_docker_call`'s other five call sites were checked); not the cause
  of that specific failure — the CI log proves this step completed and logged its "Sandbox tier
  resolved: DOCKER" success line — so not fixed in the same pass, to keep that fix's diff focused on
  the actual measured cause (import-time cost, not this real-but-separate robustness gap).
- **Phase:** future sandbox-hardening slice — wrap `docker.from_env()` here the same way the other
  five call sites already do, via `_docker_call` or an equivalent `asyncio.to_thread` + timeout.
- **Notes:** this is the only Docker-touching call in `core/sandbox.py` that doesn't already follow
  the file's own established bounded-call pattern — a small, easily-verified fix once scheduled.
### DEBT-164 [LOW · Floating] — `core/memory/semantic_memory.py`'s numpy/pyarrow imports stay eager

- **Date:** 2026-08-12
- **Reproduce:** `grep -c "lancedb\.\|pa\.\|pc\.\|np\." ailienant-core/core/memory/semantic_memory.py`
  — 48 total usages across ~30 methods/functions before this entry's partial fix; only the 13
  `lancedb.connect(...)` call sites and 2 `litellm.aembedding(...)` call sites were deferred (12.10
  round-2 CI-latency fix). `import numpy as np`, `import pyarrow as pa`, `import pyarrow.compute as pc`
  remain at module top level.
- **File(s):** `ailienant-core/core/memory/semantic_memory.py:25-27`.
- **Error:** these three imports still cost real (if smaller than litellm/lancedb) startup time on
  every process boot that reaches `api/memory_dashboard.py` (which top-level-imports this module),
  even for a caller that never touches embeddings.
- **Blocked by:** nothing technical. Not fixed in the same pass: `pa`/`pc`/`np` are used far more
  pervasively than `lancedb`/`litellm` were (~35 remaining usages, not a clean handful of call sites),
  spread across most of `SemanticMemoryManager`'s ~30 methods plus several module-level functions
  (`_chunk_schema_for_dim`, `pca_project_2d`, `_vector_of`) — deferring all of them correctly, without
  missing a site in a file this central to GraphRAG, needs its own careful pass, not one folded into
  a CI-timeout fix already touching 7 other files.
- **Phase:** future import-latency follow-up, same pattern as this entry's sibling fix — either defer
  each remaining usage to its point of use, or (cleaner, given the usage density) apply the same
  lazy-accessor pattern `core/tool_rag.py::ToolRAGStore._ensure_connected` now uses, generalized to a
  shared module-level helper if `SemanticMemoryManager` grows more lancedb-adjacent heavy imports.
- **Notes:** `numpy` alone is unlikely to be a major contributor (fast-importing relative to
  litellm/lancedb); `pyarrow`/`pyarrow.compute` are the more plausible remaining cost, partially
  overlapping with whatever `lancedb`'s own transitive imports would have pulled in anyway.


### DEBT-160 [HIGH · RESOLVED 2026-08-04, 12.15] — `_WindowsPtyBackend.terminate_tree()`/`.wait()` called pywinpty with the wrong signatures

- **Date:** 2026-08-04 · **Resolved:** 2026-08-04 (12.15)
- **Was:** `core/pty_session.py::_WindowsPtyBackend.terminate_tree()` called `self._pty.kill()` with no
  arguments, and `.wait()` called `self._pty.wait(timeout)` with a positional timeout — but
  `winpty.PtyProcess.kill(self, sig)` requires a signal, and `.wait(self)` takes no timeout argument
  at all (it busy-polls `isalive()` unconditionally). Both calls would raise `TypeError` at runtime.
  Invisible for two independent reasons: `mypy.ini` explicitly ignores the `winpty` import
  (`ignore_missing_imports`), and `pywin32` — a sibling dependency — had no `sys_platform` marker in
  `requirements.txt`, so a clean install on a non-Windows CI runner would fail outright; the practical
  effect on Windows dev machines was that `pywinpty` itself was never actually verified installed, and
  `_default_backend_factory` silently degrades to `_PipeBackend` on any exception constructing
  `_WindowsPtyBackend` — so a broken/absent pywinpty never surfaced as a crash, only a silent
  downgrade to a non-TTY transport. Surfaced by 12.15 fixing the sibling `pywin32` marker, which
  caused `pip install -r requirements.txt` to actually install `pywinpty` for the first time in this
  dev environment — pyright then resolved its real stubs and flagged both call sites.
- **Resolved:** `terminate_tree()` now calls `self._pty.kill(signal.SIGTERM)` — Windows' `os.kill()`
  maps `SIGTERM` to `TerminateProcess`, already an immediate hard kill with no POSIX-style
  graceful/forceful staging needed. `wait()` reimplements a bounded poll over `isalive()` (`PtyProcess`
  provides no timeout parameter itself), mirroring `_UnixPtyBackend.wait()`'s timeout contract.
- **File(s):** `core/pty_session.py`.
- **Verified:** two new real-backend tests in `tests/test_phase7_19_0_pty_session.py`
  (`test_real_windows_echo`, `test_real_windows_kill_reaps_process`) exercise the actual ConPTY
  backend end-to-end — previously `_WindowsPtyBackend` had **zero** test coverage on any platform
  (the existing "real backend" tests are Unix-only, `skipif`'d on Windows). `pyright`/`mypy`/`ruff`/
  full suite green.

### DEBT-014 [LOW · Blocked] — brain/swarms.py: NodeInputT add_node type-var — 6 residual ignores (measured 2026-08-03, 12.5)

- **Date:** 2026-06-05 · **Re-measured:** 2026-08-03 (12.5) — still blocked, re-logged
- **Root cause:** LangGraph's `add_node` binds `NodeInputT` with `bound=StateLike`
  (`TypedDictLikeV1 | TypedDictLikeV2 | DataclassLike | BaseModel`, per
  `langgraph/typing.py:45`). A node function typed `(state: Dict[str, Any]) -> ...` infers
  `NodeInputT = dict[str, Any]`, which is **not** a TypedDict and violates the bound →
  `type-var` error at the `add_node` call site.
- **Partial resolution (2026-06-08, Phase 8.0.4):** `tool_rag_select_node` (the node defined
  locally in `swarms.py`) was retyped `(state: AIlienantGraphState)`. `AIlienantGraphState` IS a
  TypedDict → satisfies the bound → `swarms.py:155` no longer needs an ignore (removed). This
  closed the strict/non-strict discrepancy that was the last `mypy --strict main.py` residual.
  **`mypy --strict main.py` → 0 as of 8.0.4.**
- **Residual — corrected count and site list (12.5):** this entry previously said "3 residual
  ignores" while its own prose already named 4 sites (a stale count nobody had corrected). The
  actual current count is **6**: `brain/swarms.py:157` (`run_coder_node`), `:218`
  (`run_researcher_node`, added after this entry was written — not previously listed), `:219`
  (`run_planner_node`), `:228` (`run_analyst_node`), and `brain/ideation.py:215`
  (`run_analyst_node`), `:216` (`run_synthesis_node`, also not previously listed). All six are
  **USED** (suppress real errors) under both `mypy .` and `mypy --strict` — no `unused-ignore`,
  gates stay green.
- **Why not fixed (2026-06-08) and re-verified still blocked (2026-08-03, 12.5):** two approaches
  were tried and rejected in 8.0.4:
  1. **Retype signatures to `AIlienantGraphState`** — originally cascaded to 63 `arg-type` errors
     across 19 files. Re-measured at 12.5 by actually performing the retype in a throwaway working
     copy (not assumed): **78 errors across 24 files today** — *worse*, not better, since more
     production and test call sites have accumulated passing a plain `dict` in the interim
     (`tests/test_analyst_agent.py`, `tests/test_action_log_narration.py`, `agents/logic.py`, and
     21 others). Reverted; too invasive.
  2. **`input_schema=AIlienantGraphState` on the `add_node` call** — mypy reports `Cannot infer
     value of type parameter "NodeInputT"` because it cannot reconcile a `Dict[str, Any]`-typed
     action with `StateNode[AIlienantGraphState, ...]`. Does not work.
- **Proposed enterprise refactor (deferred):** when the agent-node call sites are themselves
  hardened (a dedicated phase), retype `run_coder_node` / `run_planner_node` / `run_analyst_node` /
  `run_researcher_node` to `AIlienantGraphState` **and** migrate all direct callers (tests +
  `logic.py`) to construct a typed state (or a small `cast` helper). Alternatively, adopt it when
  LangGraph ships a stub that accepts `Mapping[str, Any]` for `NodeInputT` — confirmed still absent
  in `langgraph==1.2.11` (re-verified 2026-08-25). Until then the 6 ignores are the correct, minimal,
  gate-green suppression.
- **Notes:** The enforced gate (`mypy .`) and the campaign gate (`mypy --strict main.py`) are both
  **0** with these ignores in place. This is no longer a strict-gate blocker — only a code-cleanliness
  residual. Manifest 12.5 explicitly permitted re-logging this rather than forcing a close.
- **Re-verified 2026-08-25:** `mypy --warn-unused-ignores` against all six sites confirms every one
  still suppresses a real error — the count of 6 is accurate, not stale. Run project-wide during the
  same pass as a broader stale-ignore audit, which found and removed 94 other ignores mypy proved
  unnecessary elsewhere in the tree (9 more kept deliberately — platform/optional-dependency-gated,
  unused on this checkout but required on the Linux CI runner or without an optional package
  installed).


### DEBT-012 [LOW · RESOLVED 2026-08-03, 12.5] — diff highlighting disables word-level diff (no intra-line token slicing)

- **Date:** 2026-06-05 · **Resolved:** 2026-08-03 (12.5)
- **Reproduce (original):** Apply an edit that changes part of a line; the line shows full-line syntax color but
  no word-level add/remove shading (the per-word green/red highlight).
- **Premise corrected during 12.5 investigation:** the original entry's mechanism was wrong.
  `react-diff-viewer-continued`'s `renderWordDiff` **reconstructs the full line** and calls
  `renderContent(fullLine)` once — it never calls it per word fragment. The library already has a
  supported path for exactly this case: when the returned element carries
  `dangerouslySetInnerHTML`, it overlays its own `<ins>`/`<del>` word-diff markup onto that HTML by
  character offset (`applyDiffToHighlightedHtml`) instead of discarding the renderer. The prescribed
  fix (intersecting per-fragment `DiffInformation` offsets against `ASTToken` runs) was solving a
  problem the library doesn't actually have.
- **Resolved:** `DiffBlock.tsx`'s `renderContent` now returns `tokensToHtml(tokens)` — an
  HTML string of `<span style="color:...">` runs — via `dangerouslySetInnerHTML`, with
  `disableWordDiff={false}`. `scopeColor` resolves to a closed, curated set of
  `var(--vscode-…, #hex)` strings (never caller-controlled), so only token content needs escaping,
  which is done with exactly the five entities the library's own `decodeEntities` recognizes (it
  also accepts `&#x27;`/`&nbsp;`, simply never emitted here) — emitting any other entity would
  desync its character-offset math and misplace the word-diff highlights.
- **Dependency coupling contained, not ignored:** this does depend on an internal (undocumented)
  behavior of a third-party library. `react-diff-viewer-continued` was pinned to its exact installed
  version (`package.json`, dropped the `^4.2.2` caret) so the behavior cannot shift on a routine
  `npm install`, and a characterization test (`WD3`) renders a real changed line through the actual
  library and asserts the `<ins>` boundary lands on the correct character — engineered to fail loudly
  the moment a version bump changes the internal contract, rather than let a mis-render pass silently.
- **File(s):** `ailienant-extension/src/workspace/components/DiffBlock.tsx`; `package.json` (version
  pin); new `src/test/diffBlock.test.ts` (`WD1`–`WD4`).
- **Notes:** a line over the library's own `MAX_LINE_LENGTH` (500 chars) still falls back to plain
  text with neither highlighting nor word diff — a pre-existing library limit, documented, not fixed.

### DEBT-011 [LOW · RESOLVED 2026-06-20, 8.10.9] — test_v3_tracemalloc heap-baseline ceiling is structurally broken

- **Date:** 2026-06-04 · **Resolved:** 2026-06-20 (8.10.9)
- **Reproduce (original):** `cd ailienant-core && .\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q`
- **File(s):** `tests/test_phase3_checkpoint_gate.py` (`test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline`).
- **Error (original):** The test took the `tracemalloc` baseline snapshot immediately after `tracemalloc.start()`, collapsing the ceiling to ~64 KB while 50 `MCTSNode` + 50 `MissionSpecification` objects retained ~210-240 KB (Pydantic schema caches irrecoverable after first use). Assertion always failed.
- **Resolved:** replaced the absolute ceiling with a **self-calibrated two-pass approach** — a calibration lifecycle run measures the one-time interpreter/schema-cache residual (`calibrated_delta`); the test-cycle run then asserts `delta_bytes <= int(max(calibrated_delta, 0) * _HEAP_HEADROOM_RATIO) + _HEAP_NOISE_FLOOR_BYTES` (`_HEAP_HEADROOM_RATIO = 1.20`, floor = 64 KB). A real allocation leak still shows a monotonically growing delta across passes; one-time process-wide cache churn is absorbed by the calibration. Test is green with no skip marker.

### DEBT-007 [LOW · RESOLVED 2026-07-27, 11.8] — Auto-accept low-risk edits pays a full HITL round-trip (shift-left candidate)

- **Resolved:** 2026-07-27 (11.8). The `autoAcceptLowRisk` preference now rides the
  `TaskPayload` client→host→backend (`Workspace.tsx` → `workspace_panel.ts` → `session.ts` →
  `core/task_service.py`). In `_run_coding_task`'s HITL branch the backend judges each edit on
  its **added diff lines** via the new shared `permissions.py::scan_risk_patterns`; when the
  preference is set and no added line trips `_RISK_PATTERNS`, it applies server-side and emits
  **no** `server_hitl_approval_request` (the blast-radius gate still guards both paths). The old
  webview short-circuit (`useWSMessageHandler.ts`, vacuously true against a never-sent
  `risk_metrics` field — it auto-accepted *everything*) was removed. Follow-up: DEBT-125.
- **Date:** 2026-06-02
- **Reproduce:** N/A (latency, not an error). With auto-accept ON, every low-risk approval still flows
  backend → WS `server_hitl_approval_request` → webview → `HITL_RESPONSE` → host → WS
  `client_hitl_response` → backend before the edit applies.
- **File(s):** `ailienant-extension/src/workspace/Workspace.tsx` (auto-accept gate in the
  `server_hitl_approval_request` handler); `ailienant-extension/src/workspace/workspaceStore.ts`
  (`autoAcceptLowRisk`).
- **Error:** Per-step network RTT for actions the user pre-authorized. `O(1)` per step but one full
  round-trip each — avoidable.
- **Blocked by:** A client→host→backend channel that carries the auto-accept preference (none exists
  today; the setting is webview-local).
- **Phase:** A future shift-left optimization (Phase 11 or a later 7.14.x): the backend reads the
  auto-accept setting and, for low-risk edits, **omits emitting the approval event altogether** — the
  edit applies server-side with no round-trip.
- **Notes:** The conservative risk gate (any medium/high metric forces the manual card) must be
  preserved if/when this moves server-side.

### ~~DEBT-005~~ [RESOLVED 2026-06-29 · 8.10.19] — Multiple brain/ + agents/ interior nodes: unknown strict debt

- **Date:** 2026-05-31
- **Updated:** 2026-06-13 — Phases 8.4 and 8.7 shipped. Verification confirms **4 errors remain**
  in `brain/engine.py` under `--strict`. Entry reclassified from Floating (8.4/8.7) to **Unscheduled**.
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l`
  (returns 4 as of 2026-06-13)
- **Files:** `brain/engine.py`, `brain/ideation.py`, `brain/guardrails.py`,
  `brain/intent_router.py`, `agents/coder.py`
- **Error:** Various — `type-arg`, `no-any-return`, `no-untyped-def`. 4 confirmed in `brain/engine.py`;
  full count in remaining files not yet measured.
- **Blocked by:** Nothing structural — upstream silenced modules were unsilenced in 8.1–8.7.
  The errors are now directly measurable and fixable; they simply lack a scheduled phase.
- **Phase:** Unscheduled — assign to the next available typing-hardening sub-phase.
- **Notes:** Do NOT fix preemptively in unrelated tickets. Assign to a dedicated typing-hardening
  slice once 8.8 core work stabilizes.

---

### DEBT-147 [LOW · RESOLVED 2026-08-25, 13.1.1] — `symbol_definitions` catalog was populated only by the reactive (per-save) indexing path

- **Date:** 2026-07-27 (12.13 planning) · **Resolved:** 2026-08-25 (13.1.1)
- **Was:** `symbol_definitions` (the catalog `find_symbol_callers`/`resolve_active_skills` and other Tier-2 lookups read) was written by `ReactiveIndexer._process_change` (`core/indexer.py`) on every per-save reindex, via `upsert_symbol_definitions`. `LazyIndexer._run`'s bulk crawl — the path that runs on a cold workspace open — indexed files (`upsert_indexed_file`) and their import edges (`upsert_dependencies`), but never called `upsert_symbol_definitions` at all, so a freshly cold-indexed workspace had an EMPTY symbol catalog until every file was individually re-saved at least once.
- **Resolved:** added the same advisory, non-blocking `upsert_symbol_definitions(file_path, result.symbols, project_id)` call (wrapped in its own try/except, mirroring `ReactiveIndexer`'s existing defensive pattern — a catalog write failure must never block the canonical index/embed flow for that file) to `LazyIndexer._run`'s per-file success branch, immediately after the existing `upsert_dependencies` call.
- **File(s):** `ailienant-core/core/indexer.py` (`LazyIndexer._run`).
- **Verified:** new `tests/test_indexer_warmup.py::test_full_crawl_populates_symbol_catalog` (asserts `upsert_symbol_definitions` is awaited once per successfully indexed file, with the correct `(file_path, symbols, project_id)` args) and `::test_full_crawl_survives_symbol_catalog_write_failure` (a raised exception from the catalog write does not abort the crawl or block `broadcast_indexing_complete`) — both confirmed to fail against the pre-fix code (0 calls recorded) and pass against the fix. Full existing indexer suite (`test_indexer_warmup.py`, `test_phase8_2_6_warmup_gate.py`, `test_reactive_index.py`) re-run green, 24 passed.
- **Notes:**
### DEBT-120 [LOW · RESOLVED 2026-08-25, 13.1.2] — Two persisted telemetry tables grew unbounded, never pruned

- **Date:** 2026-07-24 (11.4 planning) · **Resolved:** 2026-08-25 (13.1.2)
- **Was:** `request_latency`/`container_lifecycle`/`action_token_usage` are append-only telemetry tables (one row per task, per container lifecycle event, per calibrated LLM call respectively) with no retention GC wired in.
- **Deeper finding on verification:** the premise assumed these tables were actively growing. They weren't. `core/telemetry.py` keeps its SQLite handle in a module global `_conn`, set only by `init_telemetry_db()` — a function `main.py`'s lifespan never called. Every write silently no-op'd at `if _conn is None: return`, so the three tables were empty in every production deployment, and the three REST endpoints reading them (`/telemetry/routing`, `/telemetry/oom`, `/telemetry/latency`) were permanently dark. `action_token_usage` never filling also meant DEBT-045's per-action calibration substrate could never accumulate a sample.
- **Resolved:** added `init_telemetry_db(TELEMETRY_DB_PATH)` to `main.py`'s lifespan startup (paired with `shutdown_telemetry_db()` at shutdown) so the store is actually live — closing both the original retention gap and the write-path defect it was hiding. Added `purge_old_telemetry()` to `core/telemetry.py` (owns `_conn`/its lock) and wired it into `core/janitor.py::run_janitor` as a third GC pass alongside vector and graph GC, deleting rows older than the shared `retention_days` window from all three tables.
- **File(s):** `core/telemetry.py`, `core/janitor.py`, `main.py`, `shared/config.py` (new `TELEMETRY_DB_PATH`).
- **Verified:** `tests/test_telemetry_retention.py` (4 cases: old rows purged/recent kept, no-op when uninitialized, the janitor wrapper, and the lifespan actually setting `_conn`) plus a live `docker compose up` run confirming `Telemetry DB initialized at /home/ailienant/.ailienant/telemetry.sqlite` in the container log.
### DEBT-202 [MEDIUM · RESOLVED 2026-08-25, 13.1.2] — `TestClient(main.app)`-based test fixtures were not isolated from the real, persistent MCP catalog DB

- **Date:** 2026-08-25 (found investigating DEBT-201's flake) · **Resolved:** 2026-08-25 (13.1.2)
- **Was:** any test whose fixture built `TestClient(main.app)` (`tests/e2e/conftest.py::e2e_client`, `tests/test_memory_dashboard.py`'s own `client` fixture, others) ran the real FastAPI lifespan, which calls `autoconnect_enabled_mcp_servers()` against `DB_CATALOG_PATH` — defaulting to the real, non-test-isolated `AILIENANT_HOME / "catalog.sqlite"`. On a machine whose real catalog has an `enabled` MCP server needing network access this environment doesn't have, the connection attempt failed mid-flight and tripped a genuine anyio task-group teardown bug in the `mcp` SDK's `stdio_client`, surfacing as `RuntimeError: Attempted to exit a cancel scope...` in whichever unrelated test ran next. Environment-dependent, not random — reproduced under two different test names (`test_memory_dashboard.py::test_purge_rejects_bad_project_id`, `tests/e2e/test_ssot_apply_patch_e2e.py`'s two cases).
- **Resolved:** an autouse `tests/conftest.py` fixture (`_isolate_mcp_autoconnect`) patches `autoconnect_enabled_mcp_servers` to a no-op at both of its call sites — `main.py`'s own module-level binding (imported at load time, a separate reference from the source module's) and `tools.mcp_adapter`'s own attribute (which `core/task_service.py`'s per-task reconcile re-imports fresh on every call, so patching the shared attribute reaches it). `tests/test_mcp_handshake.py` and `tests/test_command_menu_config.py`, which call the real function directly to test it, override the fixture locally (same name) to restore the real behavior for their own tests only.
- **File(s):** `tests/conftest.py`, `tests/test_mcp_handshake.py`, `tests/test_command_menu_config.py`.
- **Verified:** new `tests/test_mcp_autoconnect_isolation.py` (2 cases, one per call site, spying on `core.db.list_mcp_servers` to prove neither reaches the real catalog); `tests/test_memory_dashboard.py` and `tests/e2e/test_ssot_apply_patch_e2e.py` re-run clean; `DEBT-201`'s own litellm-mock-leak hardening (`tests/conftest.py::_guard_litellm_patch_leakage`) stays — unrelated but harmless, independent test-isolation hygiene.

### DEBT-203 [MEDIUM · Floating] — No adaptive execution-depth decision exists; every coding turn runs the full researcher+planner+WBS pipeline regardless of task triviality

- **Date:** 2026-08-26
- **Context:** `docs/OUTPUT_BUDGET_BRIEF.md`'s investigation (13.1.3) added the Effort Budget (`light`/`balanced`/`deep`, `core/execution_mode.py`) as a manual verification-depth preference — it controls the lint gate, self-heal retry ceiling, and whether `run_checks` executes the plan's own acceptance checks. It does not touch which agents run or how much context gathering happens beforehand.
- **The gap:** `route_after_summarize` (`brain/engine.py`) branches only on the frontend's `planner_mode_active` flag — every non-ideation coding turn runs researcher (retrieval + routing cascade) + planner (full `MissionSpecification` draft) + the complete WBS loop, whether the request is "rename this variable" or "build a complete landing page." A one-line typo fix pays the same architect-pipeline cost as a multi-file feature.
- **Why not fixed here:** the Effort Budget gives the user *manual* control over verification depth; building a real triviality classifier that adaptively skips researcher/planner for small edits is a materially larger, separate design (needs a principled trigger, likely TCI-adjacent, and a fast-path graph shape to route into) — out of this batch's scope per the brief's own framing.
- **File(s):** `ailienant-core/brain/engine.py::route_after_summarize`, `ailienant-core/agents/researcher.py`, `ailienant-core/agents/planner.py`.

### DEBT-204 [LOW · Floating] — Output-budget-brief candidate directions deferred by measurement, not built

`docs/OUTPUT_BUDGET_BRIEF.md` §8 ranked several candidate fixes; three were measured and ruled out for this batch rather than implemented:
- **§8.1 (incremental/lazy plan materialization):** the brief's own M4 measurement showed a complete, valid `MissionSpecification` costs 764–1550 tokens — the monolithic plan shape is marginal at a 4096-token window, not structurally non-viable, and N1 (explicit, runtime-probed `num_ctx`) restores 4–30× headroom on the same hardware. The blast radius (`MissionSpecification` contract, `immutable_wbs` baseline, `ValidateWBSDependenciesTool`, `PlanDocumentPayload`, `ExecutionChecklist.tsx`) is not justified by the evidence as it stands. Revisit if a much larger plan (dozens of steps) or a much smaller window makes the monolithic cost binding again.
- **§8.3 (grammar-constrained decoding / GBNF):** treats a symptom (invalid-shaped output) of a cause N1 (the real context window) removes more directly; also backend-specific and adds its own capability-detection surface.
- **§8.5 (slim the output schema — terser wire keys, no prose the coder re-derives, expanded locally after parse):** a real token-hygiene improvement, but touches the `MissionSpecification`/`WBSStep` wire contract (additive-only per charter §10) for a marginal saving now that N1 has restored real headroom. Revisit if the window pressure returns.
- **§8.9 (streaming + incremental structured parse — know at token N that the budget will run out, degrade deliberately):** the brief explicitly flagged this as "the same architectural move DEBT-194 flagged for liveness detection" — the two should land together rather than as two separate streaming-introspection layers. See DEBT-194.

### DEBT-205 [LOW · Floating] — `run_checks` (deep Effort Budget) executes only the mechanically-executable subset of a plan's own `checks`

`brain/checks_gate.py::match_executable_command` pattern-matches a check string against pytest/mypy/ruff/npm-shaped commands and runs only those via the existing guarded-command path (`tools/execution_tools.py::run_guarded_command`). A `checks` entry that names a non-command criterion (e.g. "verify FeatureCard receives its props correctly") is not executed — it is silently excluded from `check_results` rather than run. This is declared MVP scope, not a silent gap: a failing executed check now correctly blocks a "success" report (closes §8.7/M9's actual failure mode), and nothing claims the unexecuted checks passed. A fuller fix would need either an LLM-judged verification pass per unexecuted check, or a stricter authoring contract requiring every `checks` entry to be a real, mechanically runnable command.
- **File(s):** `ailienant-core/brain/checks_gate.py`.

### DEBT-206 [LOW · Floating] — Zero `server_activity_event` fired during an entire 12-minute planner window (OQ-6, out of scope)

Discovered incidentally in `docs/OUTPUT_BUDGET_BRIEF.md`'s forensic log review (unrelated to the two failures the brief investigated) and deliberately not investigated per the brief's own §10.9 instruction ("do not let it absorb this task"). The planner emits `critic_review`/`unwrapping_schema` narration per attempt, but none reached the WS `server_activity_event` channel for the whole window — either `narrate` was absent from `config.configurable` on the resume path, or events were dropped. Needs its own short, separate investigation.
- **File(s):** `ailienant-core/agents/planner.py`, `ailienant-core/brain/agent_context.py` (narration wiring).

### DEBT-207 [LOW · RESOLVED 2026-08-26, 13.1.3] — Native-thinking capability was guessed from a hardcoded substring list instead of asked from the runtime (third occurrence of DEBT-013's failure class)

- **Date:** 2026-08-26 · **Resolved:** 2026-08-26 (13.1.3)
- **Was:** `_NATIVE_THINKING_MODEL_HINTS` (`tools/llm_gateway.py`) matched a model name against a hardcoded substring list (`claude-3-7`, `deepseek-r1`, `qwq`, `o1`, `o3`) to decide whether to request native reasoning tokens. A model actually capable of it but absent from the list (confirmed live: Ollama's `/api/show` reports `capabilities: ["completion","tools","thinking"]` for `gemma4:e4b`, which the list did not cover) silently took the non-native branch — a separate, simulated prose narration pass substituted for the model's own reasoning channel.
- **Resolved:** `supports_native_thinking(target)` now probes the runtime's own declared capabilities first (`core/config/model_resolver.py::probe_runtime_capabilities`), falling back to the substring hint list only when no live probe is available (a remote/cloud target, or the runtime is unreachable).
- **Notes:** this is the same failure class DEBT-013 (resolved 2026-06-19) named — guessing a model's capability from its name instead of asking the runtime — recurring in a different code path (native-thinking detection, not `response_format` support). Logged with its history so a fourth occurrence is easier to recognize.
- **File(s):** `ailienant-core/tools/llm_gateway.py`, `ailienant-core/core/config/model_resolver.py`.

---

**DECISION RECORDS**

---

### DEBT-010 [DECISION] — OCC version-vectors on the graph state dict: rejected in favor of existing reducers (decision record)

- **Date:** 2026-06-03
- **Reproduce:** N/A (architecture decision, not an error). Architect upgrade #5 requested strict version-vector OCC on the LangGraph state dict (reject-and-retry, idempotent nodes).
- **File(s):** `brain/state.py:241-289` (per-file `document_version_id` OCC), `brain/state.py:458-459` (LangGraph reducers: `operator.add`, last-writer-wins `merge`); `agents/coder.py` (emitted `base_hash` stale-guard).
- **Error:** Not a defect — a **conflict surfaced per CLAUDE.md §3.** OCC already exists at the file granularity that governs mutation safety, and the graph state is managed by **reducers** that *merge* the concurrent `Send()` fan-out a version-vector model would *abort* — opposite strategies for the same contention. A parallel OCC layer would either duplicate the guarantee or serialize (break) the SWARM fan-out.
- **Blocked by:** N/A — **resolved as Option A (Pivot):** the intent (zero state-corruption under concurrency) is treated as already satisfied; the 7.18.6 gate row **OCC1** *asserts* the existing reducer + `base_hash` guarantee rather than adding a mechanism.
- **Phase:** Decision recorded under **7.18 (ADR-746)**. Re-open **only** if a demonstrated corruption bug proves reducers insufficient.
- **Notes:** A genuine future risk: once 7.18 wires execute-tier dispatch, **async MCP tool calls** mutating state mid-node could warrant Option B (targeted execute-tier write idempotency) — a small hardening, not a global OCC rewrite.

### DEBT-131 [DECISION] — 7 tools deliberately left unwired in `core/tool_registry.py` (decision record)

- **Date:** 2026-07-30 · **Re-audited and reduced from 11 to 7:** 2026-08-31 (8.20.6)
- **Reproduce:** N/A (architecture decision). Division 8.18 built a reachability gate asserting every `BaseTool` class is either resolvable or explicitly excluded.
- **The 8.20.6 re-audit — checked against the code, not against the recorded rationale. Seven held; four did not:**
  - **Hold, unchanged:** `atomic_code_patch`, `file_write`, `generate_docstring` are genuinely redundant with `brain/agentic_cell.py::apply_granular_edit`, which also covers new-file creation via its empty-anchor path. `guard_env_file` emits its own content-hash-idempotent HITL gate and must not be double-gated.
  - **Wired (2):** `task_list`, `task_stop`. They lived in `tools/gateway_tools.py` while `task_create`/`task_get` lived in `tools/execution_tools.py`, and inherited an orchestrator-only audience from their module rather than from their function — `task_create`'s own `owner_role` field is documented as existing "so `task_list`…", a field serving a tool nobody could call. The Coder could spawn background tasks and neither list nor kill them, leaving a hung task with no cleanup path. Now granted to `TASK_CREATE_ROLES`; `owner_role` still scopes visibility per role.
  - **Deleted (2):** `run_benchmark`, `get_benchmark_report`. Unreachable duplicates of `gateway/handlers.py`, which owns the surface and submits over loopback so the host's own single-flight and task lifecycle apply. This entry's earlier notes already named them the strongest deletion candidates; 8.20.6 executed it. Their dedicated tests went with them — coverage of deleted code has nothing to backfill (same reasoning as DEBT-208).
  - **Rationale corrected, exclusion kept (2):** `batch_semantic_edit` was recorded as "redundant with apply_granular_edit", which is **false** — it is multi-file ACID and no coder path has that; the real blocker is the missing safe `vfs_write` closure, now tracked as **DEBT-219**. `skill_invoke`'s recorded reason (role-scope disjointness) was true but not load-bearing: skills already reach the coder and planner prompts through `core/task_service.py`'s `active_skills` resolution and `core/skill_resolver.py::build_skill_directive_block`, so a tool call would re-resolve what the prompt already carries.
- **Remaining 7:** `atomic_code_patch`, `batch_semantic_edit`, `file_write`, `generate_docstring`, `guard_env_file`, `list_capabilities`, `skill_invoke`.
- **Enforcement:** `tests/test_phase8_18_checkpoint_gate.py` now iterates the allowlist rather than restating its membership, so the gate cannot go stale as the list moves; `tests/test_phase8_20_checkpoint_gate.py::test_wire3_...` additionally asserts the resolvable and excluded records stay disjoint.
- **Notes:** the lesson worth keeping is that two of the four wrong entries were wrong in the *right direction* — plausible, specific, and no longer true. An exclusion's reason has to be re-derived from the code periodically, not inherited.

---

### DEBT-165 [DECISION] — OpenSpec adoption is new-phases-only (decision record)

- **Date:** 2026-08-17
- **Reproduce:** N/A (architecture decision, not an error). `13.0` installed `@fission-ai/openspec` as a narrow, ADDED verification-gate layer (CLAUDE.md §15).
- **File(s):** `openspec/` (new); `docs/PROJECT_MANIFEST.md` (`13.0`); `CLAUDE.md` §15.
- **Error:** Not a defect. Phases 0-12 are already-closed history documented in `docs/DEV_JOURNAL.md`/`docs/DEV_JOURNAL_ARCHIVE.md` and (where one exists) their own `docs/PHASE_N_BLUEPRINT.md`. Retroactively authoring OpenSpec change proposals for closed phases would be pure migration cost with no reader benefit — nobody needs a drift-check against work that already shipped and is frozen.
- **Blocked by:** N/A — resolved as: no retroactive backfill planned.
- **Phase:** Decision recorded at **13.0**.
- **Notes:** If ever pursued, backfill is optional and low-priority — most naturally scoped as reading each closed blueprint and generating an archived (not in-flight) `openspec/specs/` entry, never as a currently-open `openspec/changes/` proposal.

### DEBT-166 [DECISION] — `openspec validate` starts advisory, not blocking (decision record)

- **Date:** 2026-08-17
- **Reproduce:** N/A (architecture decision, not an error). `.github/workflows/openspec-gate.yml` runs `npm run openspec:validate` with `continue-on-error: true`.
- **File(s):** `.github/workflows/openspec-gate.yml`; `CLAUDE.md` §15 item 3.
- **Error:** Not a defect. OpenSpec is unproven tooling in this repo as of `13.0` — making it a blocking gate on day one risks stalling merges on a tool whose noise characteristics aren't yet known, per CLAUDE.md §11's MVP-declaration process.
- **Blocked by:** N/A.
- **Phase:** Decision recorded at **13.0**.
- **Notes:** Promotion criteria not yet decided — candidates include N consecutive clean CI runs, or waiting until spec content exists beyond the `13-portfolio-level-release` pilot. Revisit once Phase 13 is further along; not scheduled to a specific sub-phase.

### DEBT-167 [DECISION] — OpenSpec CLI installed at repo-root `package.json`, not `ailienant-extension/package.json` (decision record)

- **Date:** 2026-08-17
- **Reproduce:** N/A (architecture decision, not an error).
- **File(s):** `package.json` / `package-lock.json` (new, repo root).
- **Error:** Not a defect. `ailienant-extension/package.json` is the only pre-existing first-party `package.json` in the repo, but OpenSpec governs both `ailienant-core` and `ailienant-extension` — nesting it inside the extension's devDependencies would couple a repo-wide spec tool's version to the extension's own esbuild/vsce packaging pipeline for no benefit. A dedicated root `package.json` (private, tooling-only) keeps that dependency graph fully decoupled, at the cost of one extra lockfile and one extra `dependabot.yml` row.
- **Blocked by:** N/A.
- **Phase:** Decision recorded at **13.0**.
- **Notes:** Precedented by Phase 12.17, which already added bare root-level tooling files (`dependabot.yml`, `.pre-commit-config.yaml`, `CODEOWNERS`) with no accompanying package manifest.

### DEBT-102 [DECISION] — `tree-sitter-dart` single-release supply-chain risk

- **Date:** 2026-07-03
- **Reproduce:** `pip index versions tree-sitter-dart` shows exactly one published release (`0.1.0`), with no update history since — unlike every other pinned `tree-sitter-*` package in `requirements.txt`, which have multiple releases and an active maintenance cadence.
- **Error:** accepted at 8.14.11 as the lightest viable option (no alternative Dart tree-sitter binding exists on PyPI) — a real but currently-inert risk: the package works today, but there is no signal it will be patched if `tree-sitter` core evolves incompatibly.
- **Resolution (unscheduled):** monitor for a maintained fork/successor; if the pinned wheel ever breaks against a future `tree-sitter` core bump, drop Dart support (degrade gracefully — `IMPORT_EXTRACTORS`'s unregistered-language path already handles this) rather than vendor a patched build.
- **Notes:** logged at 8.14.11 close per the project's dependency-governance stance (a new dependency's risk profile is stated explicitly, not silently absorbed).


### DEBT-145 [DECISION] — Per-task reasoning-mode config rides mutable graph state, not a config table

- **Date:** 2026-08-03
- **Reproduce:** `AIlienantGraphState` (`brain/state.py`) carries `enable_native_thinking` and
  `thinking_budget_tokens` — immutable-for-the-task-lifetime configuration — as scalar channels
  alongside genuinely mutable runtime state (`current_step_id`, `errors`, `vfs_buffer`, …). This
  follows an existing precedent (`execution_mode` does the same), but a 12.5 architecture review
  flagged the general pattern: config and runtime state living on the same substrate makes their
  different lifecycles (write-once-at-start vs. mutate-every-step) harder to reason about as more
  fields accumulate this way.
- **Error:** not a defect today. `HybridCheckpointer` promotes once per completed graph run (not per
  step) — "zero IOPS" L1 (`MemorySaver`), one L2 write per run — so these two scalars add no
  measurable I/O, and they ride alongside `vfs_buffer`, which already carries full file contents. The
  cost the review was concerned about (checkpoint bloat, per-step I/O) is not load-bearing for this
  specific pair.
- **Blocked by:** nothing technical. Deliberately deferred rather than building a `task_config` table
  for two fields: `session_state` (`core/db.py`) is a file-version tracker with no consumers that
  could be repurposed, so closing this now would mean net-new schema + migration + a §6.3
  secrets-hygiene pass to carry data that already has a safe, working home.
- **Phase:** future config/runtime-separation slice, triggered explicitly — the moment a *third*
  piece of per-task config needs to survive a restart, migrate all of it (this pair included) to a
  dedicated table in one pass, rather than adding a fourth ad hoc state channel.
- **Notes:** carved out of DEBT-079's closure (12.5) per CLAUDE.md §11.3 — the review's principle is
  accepted, the closure of DEBT-079 was not blocked on building the separation first.


### DEBT-159 [DECISION] — Pre-commit's mypy-on-changed-files is a local approximation only

- **Date:** 2026-08-04
- **Reproduce:** N/A (a design-tradeoff, not an error). `.pre-commit-config.yaml` (12.17) invokes
  `mypy` on staged files only, with `cwd` pinned to `ailienant-core/` so `mypy.ini`'s
  `explicit_package_bases`/`mypy_path` resolve correctly and the basename-collision risk the ini's
  own header warns about (`api.audit` vs `core.audit`) doesn't reappear. A partial-file invocation,
  even with correct cwd/config, is still not a full-tree guarantee.
- **File(s):** `.pre-commit-config.yaml`, `ailienant-core/mypy.ini`.
- **Blocked by:** nothing — CI's full-tree `mypy .` (`backend-gate.yml`, 12.15) is the authoritative
  gate; this hook is a speed layer only.
- **Phase:** revisit if a partial-invocation blind spot (an error only visible via a transitive
  relationship to an unchanged sibling file) is ever actually observed in practice.
- **Notes:** deliberate MVP/patch decision per CLAUDE.md §11, declared rather than left implicit.


### DEBT-177 [DECISION] — Three declared conservatisms in the tool-selection path

- **Date:** 2026-08-18
- **Reproduce:** each is a deliberate 13.0.2 tradeoff, recorded so none is later mistaken for an oversight:
  1. **`register_schema` warns instead of raising on a definition conflict.** A name re-registered with a different `privilege_tier` or `json_schema` resolves toward the stricter tier and logs a warning (`core/tool_rag.py::_merge_with_existing`). Raising would be stricter, but `populate_tool_catalog` swallows per-family exceptions, so a raise would silently amputate an entire tool family at boot — a far worse failure than a reconciled registration plus a loud log.
  2. **`_visible_eager` sizes the payload before resolvability filtering.** `core/deferred_tool_loader.py` counts `_INTENTIONALLY_UNREGISTERED` schemas (2 of `core_dev`'s 15) in `eager_chars`, inflating the estimate ~10% and biasing the decision *toward* deferred. It errs safe; correcting it would move `eager_count`/`reduction_ratio`, which fixture preconditions pin. The published break-even windows are therefore an upper bound.
  3. **Two different metrics share the name `reduction_ratio`.** The R3 gate measures against the whole catalog; `DeferredToolDecision.reduction_ratio` measures against the role slice (`prompt_size_metrics(eager, schemas)`). They are not comparable, yet `brain/swarms.py` logs the latter into `permission_audit_log`. Anyone reading that audit entry against the documented 0.70 figure will draw the wrong conclusion. The fix is an additive rename to `slice_reduction_ratio` (old key retained one release per §10).
- **File(s):** `core/tool_rag.py`, `core/deferred_tool_loader.py`, `brain/swarms.py`.
- **Error:** none are correctness defects; (3) is a live observability trap.
- **Phase:** (3) with the next audit-log touch; (1) and (2) only if their premises change.
- **Notes:** logged at 13.0.2 ship per CLAUDE.md §11.3.


### DEBT-198 [DECISION] — `pre_patch` hooks now run once per WBS step, not once per turn

Before 13.0.9, `pre_patch`/`post_patch` ran exactly once per coding turn, over the whole accumulated patch set. The new per-step apply gate (`brain/apply_gate.py::_prepare_files`/`_commit_files`) necessarily runs them once per step instead, so a hook with real cost (a lint pass, a policy check) now pays N× on an N-step turn instead of once. This is the correct tradeoff for the gate's own purpose — a step's write must not land before ITS OWN pre_patch veto is known, and by the time step 4 runs, steps 1-3 are already on disk, so a single turn-end pre_patch could no longer gate them anyway — but the added cost is real and undeclared in the hook API itself. No fix planned; revisit if a hook's per-invocation cost becomes measurable in practice.


### DEBT-208 [DECISION] — Deleting the dead topology-selector modules (13.1.3) is a permanent test-coverage reduction, not a gap to backfill

`docs/OUTPUT_BUDGET_BRIEF.md`'s M6 measurement confirmed `brain/intent_router.py`/`brain/swarms.py`/`brain/fast_path.py`/`validators/gates.py` had no production caller — the main graph (`brain/engine.py` → `alienant_app`) never dispatched into the SEQUENTIAL/MICRO_SWARM/FULL_SWARM topology they implemented. Deleting them alongside their dedicated test files (`tests/test_intent_router.py`, `tests/test_fast_path.py`, `tests/test_micro_swarm.py`, `tests/test_full_swarm.py`, plus the dead-module test functions inside `tests/chaos/test_global_crucible.py` and `tests/test_deterministic_gates.py`) removed real, passing test coverage — but coverage of code that no longer exists has nothing left to backfill. `shared/hardware.py`'s `suggested_mode` field (the same dead concept's hardware-lock half, surfaced only by the now-rewritten `HardwarePanel.tsx`) and the now-unused `VRAM_MICRO_SWARM_GB`/`VRAM_FULL_SWARM_GB` constants were removed in the same pass for the same reason. No fix planned — this is accepted as final, not a pending item.

### DEBT-109 [DECISION] — Context-utilization telemetry is flat pipe-delimited text, not typed JSONL

- **Date:** 2026-07-04
- **Reproduce:** `core/telemetry_log.py::_emit` writes every category (WS/NODE/INDEX/CONTEXT) as `CATEGORY | k=v | ...` flat text, so `core/benchmark/context_telemetry_report.py` must string-parse a brittle format — defensive `(ValueError, KeyError)` line-skipping is the mitigation, not a fix, and a `_LINE_CAP`-truncated line silently drops that record.
- **File(s):** `ailienant-core/core/telemetry_log.py`, `ailienant-core/core/benchmark/context_telemetry_report.py`.
- **Error:** accepted short-term trade-off, not a defect — the sink shipped this format for every category deliberately; the Enterprise target (migrate to one-JSON-object-per-line) is a declared future direction, not a fix pending.
- **Phase:** future telemetry-format slice.
- **Notes:**
### DEBT-149 [DECISION] — CSS's semantic-similarity term is deliberately calibrated against file-centroid distances only

- **Date:** 2026-08-03 (12.13)
- **Reproduce:** N/A — architectural decision, not an error. `search`/`search_with_paths` (`core/memory/semantic_memory.py`) compute their semantic-similarity term against file-centroid embeddings only, never the per-symbol chunk vectors 12.13 added. Chunk distances run systematically smaller than file-centroid distances, so folding them into CSS/routing without recalibration would silently inflate the score.
- **File(s):** `ailienant-core/core/memory/semantic_memory.py` (`search`, `search_with_paths`).
- **Error:** not a defect — a deliberate, reasoned calibration boundary. The entry's own framing: "correct for now."
- **Phase:** future routing-recalibration slice, if symbol-level evidence is ever worth the recalibration effort.
- **Notes:**
---

## Capability Backlog (not defects — roadmap)

*Entries here describe a capability that was never built, not a defect in what shipped — the backlog's own tier legend (above) excludes them from the open-defect count, the same way a `[DECISION]` record is excluded.*

---

### DEBT-209 [MEDIUM · Floating] — No way to change the LLM while a task is already running

- **Date:** 2026-08-28
- **Files:** `core/task_service.py` (`TaskPayload`, `_build_initial_state`), `agents/planner.py` / `agents/coder.py` (the two `resolve_model_alias_for_routing` consumers), `ailienant-extension/src/shared/config.ts` (`InferenceTier`).
- **Capability (not built):** the user asked to be able to switch the model whenever they want, *including mid-task*. Today the tier is captured in the submit payload and the routing decision is frozen onto `context_metrics` for the whole turn; both agents read that same frozen value, so a long local generation cannot be moved to a faster or stronger model once it has started — the only recourse is abort and resubmit.
- **Why it is not a defect:** nothing shipped claims otherwise. `InferenceTier` (`LOCAL_ONLY`/`HYBRID`/`SOLO_CLOUD`) looks like a live control but is a **frontend-only type with no backend consumer at all** (verified) — a pre-submit policy hint, not a model pin.
- **What it would take:** a live control channel into a running graph. Either a mutable per-session channel that a per-node model resolver reads at each node entry (cheap, but a mid-turn tier swap changes the semantic-cache key and the resolved output budget, so both need re-deriving at the boundary), or cancel + re-dispatch onto the existing checkpoint (reuses the abort mesh and Rewind, at the cost of losing the in-flight node's work). The first is the better product; the second is the smaller change.
- **Phase:** unscheduled — a real feature slice, sized after v1.

---

### DEBT-210 [MEDIUM · Floating] — No automatic subsystem/community detection in internal GraphRAG

- **Date:** 2026-08-28
- **Files:** `ailienant-core/core/memory/graphrag_extractor.py`, `ailienant-core/core/memory/semantic_memory.py`, `ailienant-core/api/memory_dashboard.py` (Nebula — see DEBT-111/113/114).
- **Capability (not built):** the internal GraphRAG ranks neighbors by PPR/`degree_centrality` and renders `file`/`external-dep` nodes in the dashboard Nebula, but never partitions the graph into subsystems. "What are this codebase's natural boundaries" has no computed answer — it is inferred by a human reading `docs/PROJECT_MANIFEST.md`'s phase divisions, not derived from the graph itself.
- **Why it is not a defect:** nothing shipped claims otherwise; 8.14.5's architecture-overview digest tool is a file-tree/PPR summary, not a clustering result.
- **External validation:** the `graphify` dev-tool (installed 2026-08-28, Claude Code tooling only — not wired into the product) runs Leiden clustering via `networkx` deterministically over its own AST-only graph, `Token cost: 0 input · 0 output` per its own `GRAPH_REPORT.md` — 400 communities over 12,756 nodes on this exact repo. Confirms the technique is cheap and local, not the heavier LLM-based community-summarization Microsoft's GraphRAG uses (the same class of finding as the GraphRAG-MCP precedent already recorded in `docs/SCHEMA_EVOLUTION.MD`).
- **What it would take:** a `networkx`-based Leiden or Louvain pass (Louvain already precedented — `degree_centrality` was hand-rolled at 7.13 specifically to avoid a `scipy` dependency, CLAUDE.md §9) over the existing `dependency_graph` SQLite edges, surfaced as a new node attribute (`community_id`) consumable by `get_architecture` and the Nebula's `nodeThreeObject`. No new indexer or storage engine — reuses edges that already exist.
- **Phase:** unscheduled — candidate for a future graph-intelligence follow-on division.

---

### DEBT-211 [LOW · Floating] — Internal GraphRAG has no git/PR-history awareness

- **Date:** 2026-08-28
- **Files:** `ailienant-core/core/memory/graphrag_extractor.py`; the 8.14.1 blast-radius mapper.
- **Capability (not built):** blast-radius mapping (8.14.1) is a pre-apply, working-tree-diff validator — it answers "what does *this uncommitted change* touch," not "what did PR #N touch" or "which files change together across history" (the latter is already logged separately as DEBT-091).
- **Why it is not a defect:** blast-radius was scoped at 8.14.1 specifically as a pre-apply gate; nothing shipped claims PR-level analysis.
- **External validation:** `graphify`'s MCP surface ships `list_prs`/`get_pr_impact` as first-class tools — external confirmation this is a buildable, bounded capability, not a wish-list item.
- **What it would take:** a `git log --numstat`-driven co-change edge (the same shape DEBT-091 already wants) plus a PR-scoped diff-to-blast-radius entry point (`git diff <base>...<head>` fed into the existing `_bfs_k_hop` machinery instead of the working tree) — no new graph engine, a new entry point onto the one that exists.
- **Phase:** unscheduled; naturally sequenced after DEBT-091 (same git-history data source).

---

### DEBT-212 [MEDIUM · Floating] — GraphRAG and project docs are separate context sources, never nodes in one graph

- **Date:** 2026-08-28
- **Files:** `ailienant-core/agents/analyst_context.py` (`build_agent_context`, Project tier = README digest + GraphRAG project summary + rules, per `docs/SCHEMA_EVOLUTION.MD`'s context-source table); `core/memory/semantic_memory.py`.
- **Capability (not built):** `docs/SCHEMA_EVOLUTION.MD`'s context-source table treats `graphrag`/`docs`/`readme` as three parallel, independently-budgeted sources (§ context-source table, `brain` field). A question spanning both — "which module implements the decision recorded in `PHASE_8_15_BLUEPRINT.md`" — has no graph traversal connecting them; it relies on the LLM independently recalling both slices within budget and connecting them itself.
- **Why it is not a defect:** the tiered-source design (8.7.0) was a deliberate, working architecture for a different problem (graduated context sources for a token-budgeted single turn) — it was never meant to be a unified graph.
- **External validation:** `graphify`'s cross-artifact graph (code + docs + configs in one `graph.json`) demonstrates the alternative shape — evaluated here only as an architectural reference, not adopted, mirroring the GraphRAG-MCP precedent already recorded at 8.14 planning: confirms the goal is reachable without adopting the reference implementation itself.
- **What it would take:** a bounded `documents_in`/`references` edge type from a doc-derived node (chunked by heading, not whole-file) to the code symbols/files it names. Genuinely non-trivial: doc→code linking needs either explicit markup or a cheap heuristic (path/symbol-name string matching within doc text), since the deterministic-only constraint (CLAUDE.md §5.7, no `scipy`-class dependency, no unjustified LLM pass) rules out an LLM-entity-linking pass by default.
- **Phase:** unscheduled; needs its own design spike before a build ticket (same caution 8.14.4's "ADR-as-graph" spike used).

---

### DEBT-216 [MEDIUM · Floating] — No rolled-up agentic product metrics (task completion, tool-call accuracy, cost/task, self-correction rate)

- **Date:** 2026-08-28
- **Files:** `ailienant-core/core/observability.py` (Phoenix spans), `ailienant-core/core/telemetry.py` (`tool_invocations`), `ailienant-core/gateway/ledger.py` (reserve/commit cost), `ailienant-core/brain/engine.py`/`brain/state.py` (`reflexion_guard`), `ailienant-core/core/task_service.py`.
- **Capability (not built):** the agent's own behavioral success has no computed rate anywhere. The event-level substrate already exists — Phoenix (13.1.5) traces every node/LLM span with cost and latency, `tool_invocations` logs every tool-call outcome, the ledger meters cost per turn, `reflexion_guard` drives self-healing — but none of it is aggregated into the four standard agent-eval KPIs (Task/Goal Completion Rate, Trajectory/Step Efficiency, Tool Call Accuracy, Cost per Task, Self-Correction Rate). In particular, no `goal_achieved` boolean is ever recorded per task; there is no way to answer "what % of tasks actually succeed" without reading transcripts by hand.
- **Why it is not a defect:** nothing shipped claims this rollup exists. Phoenix and the telemetry sinks were built as raw observability, not as a KPI layer on top of it.
- **External validation:** this is the standard framework production LLM-agent systems (LangSmith, Arize's own agent evals, Galileo) ship as their core value proposition — not a speculative addition. Since Phoenix is already self-hosted here, its own `phoenix.evals` module (LLM-as-judge grading) is the first thing to evaluate for `goal_achieved` scoring before reaching for a new dependency like DeepEval/Ragas (CLAUDE.md §9 — lightest viable option).
- **What it would take:** a `goal_achieved`/`steps_taken` field emitted once per task from `core/task_service.py`'s terminal state, joined against the existing `tool_invocations` and ledger cost rows by `task_id` for the rollup. Touches `core/`/`brain/` (CLAUDE.md §3 — deterministic engine, higher blast radius) so it needs its own blueprint before a build ticket, not an ad-hoc patch.
- **Phase:** unscheduled — candidate for a future observability-rollup division, if/when agent-behavior debugging (not code-quality debugging) becomes the bottleneck.

---

### DEBT-213 [MEDIUM · RESOLVED 2026-08-31, 8.20] — `web_fetch` destination guard was open to DNS rebinding

- **Date:** 2026-08-28 · **Resolved:** 2026-08-31 (8.20.7)
- **Was:** `core/url_guard.py` resolved the hostname to classify it, and httpx then resolved the same name again to connect. A name server the attacker controls can answer the two lookups differently — public for the check, private for the connection — which no amount of validation before the connect can detect.
- **Resolution — pinning, not re-checking:** `resolve_fetch_target` now returns the address it approved, and `WebFetchTool._fetch` requests that literal address while `Host` and TLS SNI keep the real hostname (`extensions={"sni_hostname": host}`). The client therefore performs no second, unchecked resolution. Applied on every redirect hop, not only the first, composing with 8.19's manual redirect walk.
- **Why this is not a TLS weakening:** verified hermetically rather than asserted — a local TLS server presenting a certificate valid only for a non-resolving name is reachable over the pinned IP *with* the SNI override and is rejected by certificate verification *without* it. `verify` is untouched. Locked by the gate's PIN3 row, which carries the instruction to revert the approach rather than relax the row if it ever needs `verify=False`.
- **Files:** `core/url_guard.py` (`FetchTarget`, `resolve_fetch_target`, `_pinned_url`), `tools/perception_tools.py` (`_fetch`, `_host_header`).
- **Verified:** `tests/test_phase8_20_checkpoint_gate.py` PIN1–PIN3; `tests/test_phase8_19_checkpoint_gate.py` unchanged and green.

---

### DEBT-214 [LOW · Floating] — DuckDuckGo search fallback parses an unversioned public HTML page

- **Date:** 2026-08-28
- **Files:** `ailienant-core/tools/mcp_adapter.py` (`make_duckduckgo_fallback_search_fn`).
- **Gap:** the fallback leg of `web_search` regex-parses DuckDuckGo's public results markup. It carries no contract and can drift silently to zero results on any markup change; its own docstring already declares this. Now that `web_search` reaches more roles, a silent degradation is felt in more places.
- **Why it is not a defect:** it is deliberately the fallback, never the default — the Brave MCP provider is primary, and the fallback degrades to the standard "unavailable" string rather than raising.
- **What it would take:** either a scheduled canary asserting the parse still yields results, or a second keyless provider so a single markup change cannot zero the capability.
- **Phase:** unscheduled.

---

### DEBT-215 [LOW · RESOLVED 2026-08-31, 8.20] — `ROLE_REGISTRY.allowed_tools` is not vestigial; its premise was wrong

- **Date:** 2026-08-28 · **Resolved:** 2026-08-31 (8.20.7)
- **Was:** recorded as dead data kept alive by a frozen test snapshot, with deletion as the prescribed fix. 8.20's plan carried that instruction.
- **Why it was reversed:** every role's `allowed_tools` entry holds `FileReadTool`, `GrepTool`, `GlobTool`, and `query_graphrag` — precisely the tools live dispatch granted to `researcher` alone until 8.20.3. The field was therefore the only artifact in the repository recording the contract the live RBAC gate had drifted away from, and it independently corroborates that division's grant. Deleting it would have destroyed that evidence. Its other entries already agreed exactly with live state (`BashTool` ↔ `_SANDBOX_BASH_ROLES`, `apply_patch` ↔ `_APPLY_PATCH_ROLES`), which is what makes the disagreement on the read tools legible as drift rather than noise.
- **Resolution:** the resolution the entry itself proposed — make the gate check the live surface. `tests/test_phase8_20_checkpoint_gate.py::test_contract1_legacy_whitelist_agrees_with_live_allowed_roles` bridges the two vocabularies and asserts that a role holding a legacy entry is not denied its live counterpart. One direction only: the legacy record may omit tools added since, but the live gate may never again silently withhold a capability the role contract names.
- **Files:** `tests/test_phase8_20_checkpoint_gate.py`; `agents/roles.py` and `tests/test_phase8_8_tool_parity_gate.py::_FROZEN_ROLE_TOOLS` deliberately unchanged.
- **Notes:** logged under CLAUDE.md §11.2 as a declared deviation from an approved plan.

---

### DEBT-219 [MEDIUM · Floating] — `batch_semantic_edit` is excluded for want of a safe write closure

- **Date:** 2026-08-31 (recorded at 8.20 ship)
- **Reproduce:** read `core/tool_registry.py::_INTENTIONALLY_UNREGISTERED["batch_semantic_edit"]` and `tools/mutation_tools.py::BatchSemanticEditTool`.
- **Gap:** the tool is a three-phase multi-file ACID transaction (pre-validate every item's OCC, apply to a local write buffer, commit — leaving the VFS byte-identical on any failure). No coder path offers that: `brain/agentic_cell.py::apply_granular_edit` is single-file and commits per path, so a cross-file refactor lands partially today. It was recorded for years as "redundant with apply_granular_edit", which is false; 8.20.6 corrected the record.
- **Why it is still excluded:** the second half of the original reason is real and unchanged — no safe `vfs_write(path, content)` closure exists in production. Writes flow through `VFSMiddleware.ingest_dirty_buffers` and the per-step apply gate, not a simple write API.
- **What it would take:** design that closure first (how a tool-issued multi-file commit interacts with `brain/apply_gate.py`'s per-step `_prepare_files`/`_commit_files` and with the cell's MCTS candidate rollback), then wire the tool. That is a division, not a flag flip.
- **Phase:** future multi-file-mutation slice.
- **Notes:** the class is deliberately NOT deleted — it is a correct implementation of a capability the system otherwise lacks.

---

### DEBT-220 [LOW · Floating] — `bind_cell_tools` has no consumer and would advertise the wrong names

- **Date:** 2026-08-31 (recorded at 8.20 ship)
- **Reproduce:** grep `bind_cell_tools` — the only hits are its own definition and docstring in `brain/agentic_cell.py`. The live path is `_default_reasoner`, which parses a JSON envelope out of text.
- **Gap:** were it wired to a real tool-calling model, `bind_tools(CELL_TOOLS)` would name each tool after its Pydantic class (`RunTerminalArgs`), while the dispatcher compares against `TOOL_NAME` (`run_terminal`) — so every native tool call would fall through to the registry-fallback branch and resolve as an unknown name.
- **Why it was left:** 8.20 made the names derivable (`_CellToolArgs.TOOL_NAME`), which is the prerequisite; wiring native tool-calling is a separate decision about whether the cell should stop parsing JSON out of text at all.
- **What it would take:** either delete the unused seam, or convert `CELL_TOOLS` into properly-named tool objects and switch `_default_reasoner` to the native path behind a capability check.
- **Phase:** unscheduled.

---

### DEBT-234 [LOW · Floating] — Injected reasoner seams declare no model, so a tool budget can be sized for the wrong one

- **Date:** 2026-09-01
- **Gap:** `brain/agentic_cell.py`'s `cell_reasoner` and `brain/nodes/subagent_worker_node.py`'s `dispatch_tool_reasoner`/`dispatch_answer_fn` are opaque `Callable`s (`CellReasoner = Callable[[Sequence[Dict[str, str]]], Awaitable[List[ToolCall]]]`). Both nodes now size their deferred-tool budget against a named tier constant derived from the default reasoner's own alias (`MODEL_BIG`), which is correct for the default path. An injected reasoner running some other model would still be budgeted against `big`'s window, and nothing in the protocol lets the caller detect it.
- **Why it was left:** there is nothing to read. Adding a model declaration means widening the reasoner protocol (a `model` attribute or a config field), which touches every injection site and every test double for a case that has no live consumer — the seams exist for test injection and a future backend swap, and both currently run the default model.
- **What it would take:** give the reasoner protocol an optional model declaration and have the tool-budget call read it, falling back to the default tier constant when absent.
- **Phase:** unscheduled.

---

### DEBT-233 [LOW · Floating] — One fixed reasoning template is imposed on every free-form call

- **Date:** 2026-09-01
- **Gap:** `tools/llm_gateway.py::_inject_reasoning_scaffold` appends a module-constant instruction to every non-native `free_form_answer=True` call, prescribing a fixed four-beat shape (what you are weighing → trade-offs → what to check → conclusion). Division 8.23 removed the analyst's OWN fixed checklist, which was stacked on top of this one, but the gateway's remains — so every reasoning surface in the app still narrates to the same rhythm regardless of what it is reasoning about.
- **Why it was left:** the scaffold is global. Every free-form caller inherits it, so changing its shape is a behaviour change across surfaces this division neither touched nor measured, and the round-to-round repetition it was blamed for had a nearer cause (identical inputs plus greedy decoding), now fixed. Removing it wholesale also risks the opposite failure — an unstructured model producing no usable narrative at all in the Thought Box.
- **What it would take:** make the scaffold a per-caller parameter with the current text as the default, so a caller that wants open-ended reasoning can opt out without altering anyone else's; then evaluate per surface whether the structure earns its place.
- **Phase:** unscheduled.

---

### DEBT-232 [LOW · Floating] — Provider-side reasoning tokens are not budgeted on the strict-JSON path

- **Date:** 2026-08-31
- **Gap:** `tools/llm_gateway.py`'s `response_format` branch (`astream_reasoning` → `ainvoke`) sets no thinking/reasoning budget, and a model that bills its own reasoning inside `completion_tokens` (Gemini 2.5 Flash, observed) can therefore spend the whole `max_tokens` allowance on reasoning and return a truncated object. The existing SAFETY INVARIANT in that method guards AILIENANT's OWN prompt scaffold, not the provider's native reasoning — a distinction the invariant's wording does not currently make.
- **Why it was left:** the incident that surfaced it is fixed at its real cause (the call was sized against a flat 8192 for a 1M-token model; see the 8.22 journal entry), so the budget is no longer tight enough for reasoning to exhaust it. Threading a `thinking` kwarg through `ainvoke` means touching the streaming path's own param-degradation and retry logic (`_THINKING_PARAM_UNSUPPORTED`), which is a wider blast radius than the residual risk justifies.
- **What it would take:** forward an explicit zero/minimal reasoning budget on the `response_format` branch, reusing the existing `_remember_thinking_unsupported` degradation memo so a provider that rejects the param self-heals once per session.
- **Phase:** unscheduled.

---

### DEBT-231 [LOW · Floating] — `planner_retry_count` and `send_telemetry` are written/defined but never read

- **Date:** 2026-08-31
- **Gap:** two dead signals found during the 8.22 seam audit. (1) `planner_retry_count` is written at four `agents/planner.py` exits and declared in `brain/state.py`, but nothing outside tests reads it — it is checkpoint weight with no consumer. (2) `api/websocket_manager.py::send_telemetry` has ZERO callers repo-wide, so `routing_warning` — including the actionable "no cloud provider is configured; staying on a small local model" text `core/memory/context_auditor.py` composes — is computed, persisted to state, and never displayed in any mode.
- **Why it was left:** each has two defensible resolutions (wire the consumer, or delete the producer and its channel), and picking wrongly for `routing_warning` costs a user-facing signal that a hardware reroute happened. That is a product call, not a cleanup.
- **What it would take:** for `routing_warning`, call `send_telemetry` from the researcher's exit or fold the warning into the existing status-bar telemetry push, then keep it; for `planner_retry_count`, delete the channel and its four writes unless a retry-rate view is wanted.
- **Notes:** neither is reachable by any current gate — `ruff.toml` enables only `["E4","E7","E9","F"]` (intra-module), and no dead-code pass (`vulture`, `ts-prune`) is wired. That gap is the general case; see the 8.22 journal entry.
- **Phase:** unscheduled.

---

### DEBT-230 [MEDIUM · Floating] — No gate validates compiled-graph integrity beyond conditional-edge path-maps

- **Date:** 2026-08-31
- **Context:** 8.22 shipped `tests/test_graph_path_map_integrity.py`, which asserts that every router's returnable string literals are declared in its own `add_conditional_edges` path-map. That closes the specific seam that made accepting a plan raise `KeyError('step_dispatch')`, but it is one property out of several the compiled graph could be checked for.
- **Gap:** still unguarded — (1) `Send()` targets are verified by eye, not by a test, against the `add_node` set; (2) `brain/state.py`'s `assert_declared_channels` never runs for `dispatch_origin`/`dispatch_fanout`/`dispatch_gate`/`dispatch_advance`, which `brain/engine.py` adds raw with no `_instrument_node` wrapper, so an undeclared channel write from those four is silently dropped even under pytest; (3) the path-map gate reads literals statically, so a router returning a computed value is invisible to it (`brain/dispatch.py::route_after_synthesis` is confined to `RETURN_NODES` for exactly this reason, but nothing enforces that pattern for a future router).
- **Why it was left:** the shipped gate covers the failure class that actually bit, and each remaining item is a separate mechanism rather than a variant of the same check.
- **What it would take:** wrap the four dispatch nodes to restore channel assertion; add a `Send`-target reachability test; consider promoting `RETURN_NODES`-style confinement to a convention the gate can check.
- **Phase:** unscheduled.

---

### DEBT-229 [LOW · Floating] — Mid-run steering is coarse on the one-shot coder path

- **Date:** 2026-08-31 (recorded at 8.21 ship)
- **Reproduce:** send a `client_steering_message` while a WBS step is executing on the one-shot coder path (a step the planner did not flag as needing iteration) rather than inside the agentic cell.
- **Gap:** `drain_steering` runs at the top of an agentic-cell iteration, and `route_after_cell` makes each iteration a graph super-step — a natural, checkpointed boundary. The one-shot path has no iteration to re-enter, so a message sent mid-step is picked up only at the next graph node boundary (planner → coder → verify). The operator sees a longer delay before the correction takes effect, and a step already generating its edit finishes on the pre-steering instruction.
- **Why it was left:** closing it means giving the one-shot path a re-entry point it deliberately does not have — it exists precisely because most WBS steps do not need a loop, and adding a drain checkpoint inside it would import the cell's super-step overhead into the cheap path. Not a defect of the steering channel; a property of where loop boundaries exist.
- **What it would take:** either a drain at the coder node's own entry (cheap, still coarse — one chance per step rather than per iteration), or routing a steered step into the cell. The first is probably right if this is ever felt in practice.
- **Phase:** unscheduled.
- **Notes:** stated in `docs/SCHEMA_EVOLUTION.MD` §60's scope-limit section too, so the contract does not read as promising per-iteration delivery everywhere.

---

### DEBT-228 [MEDIUM · Floating] — Six gateway tests depend on the host's live free RAM

- **Date:** 2026-08-31 (surfaced during the 8.20 full-suite run)
- **Reproduce:** with under ~1 GB of free physical memory, run `tests/test_llm_gateway_num_ctx.py`, `tests/test_abort_mesh.py::test_astream_byom_records_usage_on_completion`, `tests/test_hybrid_routing.py::test_ainvoke_tier_cloud_records_local_when_byom_resolves_local`, and `tests/test_llm_gateway_generation_telemetry.py::test_ainvoke_records_the_resolved_num_ctx_for_a_local_target`. Six fail with `LocalResourceExhaustedError` from `core/config/model_resolver.py::check_local_admission`. Re-running the same four files with `AILIENANT_LOCAL_RAM_SAFETY_FLOOR_MB=64` turns all 44 green — measured, which is what identifies the cause as the host rather than the code.
- **Gap:** these assert gateway behaviour (`num_ctx` resolution, usage recording, tier routing), not memory admission, yet they exercise the real `check_local_admission`, which compares live host memory against `_LOCAL_RAM_SAFETY_FLOOR_MB` (default 1024 MB). `tests/conftest.py` stubs no hardware. The suite's outcome therefore depends on what else the developer has open — a violation of the zero-flake policy in `DEVELOPERS.md` and CLAUDE.md §16.4, and one that reads as a code regression to whoever hits it next.
- **What it would take:** pin the floor (or stub `check_local_admission`) in a shared fixture — the sensitivity is one function, not one test file — sealing the heavy engine at its boundary per CLAUDE.md §8.3.
- **Why it was left here:** pre-existing and outside Division 8.20's blast radius. None of the four files is in its diff, and all four ran green earlier in the same session against the same code; only free memory changed.
- **Phase:** future test-isolation slice.

---

### DEBT-217 [MEDIUM · Floating] — No Runtime Capacity panel; the chat HUD's context-window ring reads the wrong denominator on a local target

- **Date:** 2026-08-31
- **Files:** `ailienant-extension/src/workspace/components/TelemetryHUD.tsx` (`OccContextRing`), `ailienant-core/api/sessions.py` (`compute_context_occupancy`), `ailienant-core/core/config/model_resolver.py` (`resolve_num_ctx`, `check_local_admission` — the RAM-aware/`/api/ps`-informed resolution this panel would consume), `ailienant-core/core/config/byom_config.py`.
- **Capability (not built) + adjacent real bug found while investigating it:** `compute_context_occupancy` sizes the chat HUD ring's denominator from `_resolve_model_window` (the model's **architectural** ceiling via `litellm.get_model_info()`, or a static 8192 fallback) — never the RAM-clamped `num_ctx` a local Ollama target actually gets served. `resolve_num_ctx`'s own docstring already says this explicitly. On constrained hardware the ring can read "9% full" against a number that was never the real ceiling, while the real (RAM-clamped) window is already tight — silently reproducing the exact context-shift/JSON-drift failure mode `_resolve_local_num_ctx_kwarg`'s docstring already documented once.
- **The requested capability:** a reference-only (not live/streaming) panel in the web dashboard's Runtime section. Spec, as designed with the user:
  - Top-left hamburger menu lists every configured BYOM preset; selecting one switches the panel to that preset.
  - The preset's models are listed (one row per tier), each with its estimated tokens/sec (sourced from `_measured_local_seconds_per_token`'s calibration window, `tools/llm_gateway.py`, once fed — see DEBT-192). Clicking a model switches the graph to that model's estimate.
  - The graph is a nested "matryoshka" treemap: three boxes, outermost-to-innermost — a fixed 1M-token horizon (scale anchor) → the model's architectural capacity → the hardware-resolved ceiling (`resolve_num_ctx`/`check_local_admission`'s real reading). Nesting is stylized for legibility, not strict area-proportional (a true area-proportional treemap renders the hardware box as an invisible sliver next to a 1M anchor).
  - Below the graph: a KV-cache-quantization selector (f16/q8_0/q4_0) that is a pure **what-if estimator** — selecting an option recomputes and redraws only the hardware-ceiling box using that quantization's bytes-per-token multiplier. It does **not** touch the running Ollama process (see DEBT-218, kept deliberately separate). Accurate per-quantization multipliers (roughly q8_0 ≈ half of f16's KV bytes/token, q4_0 ≈ a quarter — first-order approximations, to be validated empirically) would replace today's single blanket `_ESTIMATED_KV_BYTES_PER_TOKEN`, ideally derived per-model from architecture fields if Ollama's `/api/show` exposes them.
- **Why it is not a defect:** the HUD ring's current denominator was a working design for a cloud-only or unconstrained-hardware deployment; nothing shipped claims it reflects post-RAM-clamp reality.
- **What it would take:** the panel itself (frontend, dashboard-only, no code built this pass); once its real-capacity resolution logic exists, point `compute_context_occupancy` at it instead of the architectural max so the (separate, still-live) chat HUD ring also becomes accurate.
- **Phase:** unscheduled — a real feature slice.

---

### DEBT-218 [MEDIUM · Floating] — No way to reconfigure Ollama's KV-cache quantization from AILIENANT

- **Date:** 2026-08-31
- **Files:** none yet — no code exists for this; would touch a new OS-integration module plus whatever surfaces the action (see DEBT-217's what-if estimator, which this would upgrade from "estimate" to "apply").
- **Capability (not built):** `OLLAMA_KV_CACHE_TYPE` (and typically its prerequisite `OLLAMA_FLASH_ATTENTION=1`) are read by the Ollama **server process** at startup, not a per-request API option, and AILIENANT neither launches nor manages that process today (confirmed: no `ollama serve`/process-spawning code exists anywhere in `ailienant-core`; it is only ever a client). A user cannot ask AILIENANT to "just switch to q8_0" and have it happen.
- **Why it is not a defect:** nothing shipped claims this control exists; f16 (Ollama's default) is what any user gets who never sets the env var themselves, regardless of AILIENANT.
- **Why this needs its own design pass, not a quick patch:** actually applying this safely requires (a) detecting how Ollama is currently running on this OS — tray app, systemd service, or manual — each with a different persistence mechanism (Windows `setx`/registry, macOS `launchctl setenv`, a `systemd` drop-in on Linux), and (b) safely stopping/restarting a process that may be **shared with other tools** the user runs against the same Ollama instance — real shared-infrastructure risk with a genuine bad-failure-mode: a botched restart could leave the user with no working Ollama at all, worse than the status quo. This is exactly the class of action that needs an explicit, non-silent confirmation UX stating the shared-service risk, plus a post-restart verification read-back before ever telling the user it worked — not a bare click.
- **What it would take:** an OS-detection step, a per-OS persistence writer, an explicit confirmation dialog, a stop/restart sequence, and a verification read-back (re-probe `/api/show`/`/api/ps` for the new setting) before reporting success. A genuinely separate, larger feature from DEBT-217's read-only estimator.
- **Phase:** unscheduled — needs its own design spike before a build ticket.

---

### DEBT-221 [MEDIUM · Floating] — `run_agentic_cell_node` is CC 108 (radon grade F), the highest cyclomatic complexity in the backend

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/brain/agentic_cell.py -s -n F`
- **File(s):** `ailienant-core/brain/agentic_cell.py:396` (`run_agentic_cell_node`).
- **Error:** not a defect — a maintainability/testability risk. CC 108 means ~108 independent execution paths in one function; exhaustive branch coverage is impractical, and every new agentic-cell capability added to this node raises the number further.
- **Why it is not fixed in place:** the agentic cell is the highest-blast-radius node in the graph (CLAUDE.md §3, Core/Eval/Brain zone — demands determinism/immutability); decomposing it safely needs characterization tests written against current behavior *before* any extraction, not an opportunistic refactor riding on an unrelated change.
- **What it would take:** profile the branch structure (likely dispatch by tool-call type / cell phase), extract named per-branch helpers behind the existing node contract, add characterization tests first. Its own sub-phase with a checkpoint gate, per CLAUDE.md's phase-closure convention.
- **Phase:** unscheduled — candidate for a future node-decomposition division.
- **Notes:** filed as a batch alongside DEBT-222–DEBT-227, all six `run_*_node`/hot-path functions currently at radon grade F.

---

### DEBT-222 [MEDIUM · Floating] — `run_planner_node` is CC 84 (radon grade F)

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/agents/planner.py -s -n F`
- **File(s):** `ailienant-core/agents/planner.py:209` (`run_planner_node`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221.
- **Why it is not fixed in place:** the planner node is a graph hot path (CLAUDE.md §3 Core/Eval/Brain zone); needs characterization tests before extraction, not an ad-hoc split.
- **What it would take:** same approach as DEBT-221 — profile branch structure, extract named helpers, characterize before refactor.
- **Phase:** unscheduled — candidate for a future node-decomposition division.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-223 [MEDIUM · Floating] — `run_coder_node` is CC 80 (radon grade F)

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/agents/coder.py -s -n F`
- **File(s):** `ailienant-core/agents/coder.py:459` (`run_coder_node`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221.
- **Why it is not fixed in place:** the coder node is a graph hot path (CLAUDE.md §3 Core/Eval/Brain zone); needs characterization tests before extraction, not an ad-hoc split.
- **What it would take:** same approach as DEBT-221 — profile branch structure, extract named helpers, characterize before refactor.
- **Phase:** unscheduled — candidate for a future node-decomposition division.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-224 [MEDIUM · Floating] — `run_researcher_node` is CC 76 (radon grade F)

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/agents/researcher.py -s -n F`
- **File(s):** `ailienant-core/agents/researcher.py:201` (`run_researcher_node`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221.
- **Why it is not fixed in place:** the researcher node is a graph hot path (CLAUDE.md §3 Core/Eval/Brain zone); needs characterization tests before extraction, not an ad-hoc split.
- **What it would take:** same approach as DEBT-221 — profile branch structure, extract named helpers, characterize before refactor.
- **Phase:** unscheduled — candidate for a future node-decomposition division.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-225 [MEDIUM · Floating] — `websocket_endpoint` is CC 59 (radon grade F)

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/main.py -s -n F`
- **File(s):** `ailienant-core/main.py:1326` (`websocket_endpoint`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221. This one sits in the Gateway/Transport zone (CLAUDE.md §3 — the untrusted boundary), where high branch count also raises the odds of a missed edge case in message-type dispatch.
- **Why it is not fixed in place:** the WS endpoint is the single entry point for every client message type; splitting it needs a message-type dispatch table extracted behind characterization tests, not an ad-hoc split mid-feature.
- **What it would take:** likely a dispatch-table refactor keyed by inbound event type (mirrors the existing `ws_contracts.py` event taxonomy), each branch's body extracted to a named handler, characterized before extraction.
- **Phase:** unscheduled — candidate for a future node-decomposition division.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-226 [LOW · Floating] — `TaskService._format_coding_summary` is radon grade F

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/core/task_service.py -s -n F`
- **File(s):** `ailienant-core/core/task_service.py:1623` (`TaskService._format_coding_summary`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221. Filed LOW rather than MEDIUM: it is a summary-formatting method, not a graph hot path, so its blast radius is lower than the `run_*_node` functions.
- **Why it is not fixed in place:** out of scope for the unrelated pass that surfaced it; needs its own extraction pass with characterization tests over its formatting branches.
- **What it would take:** profile the branch structure (likely per-outcome-type summary formatting), extract named per-branch formatters.
- **Phase:** unscheduled.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-227 [LOW · Floating] — `ValidateWBSDependenciesTool._arun` is CC 53 (radon grade F)

- **Date:** 2026-08-31
- **Reproduce:** `python -m radon cc ailienant-core/tools/planner_tools.py -s -n F`
- **File(s):** `ailienant-core/tools/planner_tools.py:116` (`ValidateWBSDependenciesTool._arun`).
- **Error:** not a defect — maintainability/testability risk, same class as DEBT-221. Filed LOW rather than MEDIUM: it is a single tool's validation method, not a graph hot path.
- **Why it is not fixed in place:** out of scope for the unrelated pass that surfaced it; needs its own extraction pass with characterization tests over its validation-rule branches.
- **What it would take:** profile the branch structure (likely per-dependency-rule validation), extract named per-rule checkers.
- **Phase:** unscheduled.
- **Notes:** part of the DEBT-221 batch.

---

### DEBT-025 [LOW · Blocked] — Docker persistent-PTY backend has no daemon integration test

- **Date:** 2026-06-09
- **Files:**
  - `ailienant-core/core/sandbox.py` — `_DockerPtyBackend` (`exec_create`/`exec_start(socket=True, tty=True)` persistent shell) and `DockerSandboxAdapter.open_session`.
- **Error:** not a type error — a coverage gap. The directed suite (`tests/test_phase7_19_0_pty_session.py`) verifies the session contract through a stub backend and the real Unix `openpty` backend (Unix-only, skipped on Windows). The **Docker** session backend's real exec-socket framing (raw-stream `tty=True` semantics, socket detach on container stop, `exec_inspect` exit-code reap) is exercised only structurally via the shared `_PtySession` machinery — no test attaches to a live `ailienant-sandbox-daemon` container.
- **Blocked by:** a Docker daemon in CI (the broader sandbox-integration gap; `test_execution_tools` Docker failures are already environmental per project notes). Confirmed still true: `.github/workflows/` has only `docker-publish.yml` — no daemon-backed test lane exists.
- **Phase:** **Phase 7.19 CLOSED 2026-06-08 without resolving this** — the dispatcher shipped and drives the host PTY path first, but no CI daemon lane was ever added. Remains unowned by any phase; blocked purely on CI infrastructure (mirrors the DEBT-035 precedent for a phase closing around an unresolved entry).
- **Notes:** declared MVP during 7.19.0. The host PTY path (Native Direct) — the tier the 7.19.2 dispatcher actually drives first — is fully covered (stub + real openpty). The Docker backend is implemented for parity but unverified end-to-end against a container; treat its first live use as integration-test-gated.


### DEBT-035 [MEDIUM · Floating] — MultiPL-E TypeScript execution needs a Node-capable sandbox runtime

- **Date:** 2026-06-12
- **Reproduce:** run a TypeScript codegen problem through `SandboxCodegenExecutor.run(program, Language.TYPESCRIPT, …)` — it returns `ExecOutcome(passed=False, exit_code=-2, stderr="[unsupported_runtime: ...]")` instead of executing.
- **File(s):** `ailienant-core/tests/benchmark/executors.py` (`SandboxCodegenExecutor`); `ailienant-core/core/sandbox.py` (`_DOCKERFILE_TEXT`, `python:3.13-slim`).
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The shared sandbox image is Python-only (no Node/tsc), so MultiPL-E TS cannot be executed in-container. 8.3.1 ships the full TS *adapter* (loader, prompt, extraction, assembly, Pass@1 wiring); only the TS *execution backend* is deferred. Python (HumanEval) Pass@1 is real.
- **Blocked by:** nothing technical — needs a Node-capable sandbox tier without compromising the locked Docker security profile.
- **Phase:** **Division 8.13 CLOSED 2026-06-30 without resolving this** — the polyglot devcontainer adapter (blueprint `docs/PHASE_8.13_BLUEPRINT.md`) serves only the agent's *trusted* project execution; the untrusted MultiPL-E TS benchmark lane is the opposite threat model (§2) and permanently stays `unsupported_runtime` — pointing it at a user-owned devcontainer would dissolve the locked-cage guarantee. Remains open: a distinct **locked** Node-capable sandbox tier (mirroring `DockerSandboxAdapter`'s hardening — no network, read-only mount, non-root, env-whitelist) is still needed to execute untrusted TS candidates. No phase currently owns this.
- **Notes:** logged at 8.3.1 ship per CLAUDE.md §7.3. TS Pass@1 remains `unsupported_runtime`; the Python subset DoD holds.


### DEBT-074 [MEDIUM · Blocked] — `pre_file_read` GraphRAG-injection hook bypasses cost accounting

- **Date:** 2026-06-23
- **Reproduce:** the `hooks` table (`core/db.py`) supports only `pre_patch` / `post_patch` events, dispatched in `core/task_service.py`. A proposed `pre_file_read` event would inject a file's dependency subgraph into the agent's context automatically at read time.
- **Error:** tokens injected laterally by such a hook never pass through the CSS×TCI router (`core/memory/context_auditor.py`) or the token ledger (`core/token_ledger.py`), so they escape the FinOps gate and corrupt `savings_usd` accounting. It also turns structural context implicit (non-deterministic, hard to debug) versus the current explicit `pre_patch`/`post_patch` artifacts the agent knows are present.
- **Blocked by:** no accounting path for hook-injected context. Re-evaluate only once the router meters laterally-injected tokens.
- **Phase:** future graph-intelligence slice (post-8.14).
- **Notes:** carved at 8.14 planning per CLAUDE.md §11.3. Rejected sibling: the recursive-CTE k-hop rewrite — multi-hop BFS already exists (`_bfs_k_hop`, `_K_HOP={CLOUD:3,…}`), so it is a refactor of working code, not a missing capability; revisit only if `_bfs_k_hop` becomes a measured bottleneck at scale.


### DEBT-075 [LOW · Unscheduled] — Syntactic-only symbol extraction; no LSP-style type resolution

- **Date:** 2026-06-23
- **Reproduce:** the indexer extracts symbols and dependencies by name via tree-sitter; it does not resolve types. When the coder needs a function's return type, the LLM must infer it from the file rather than reading a resolved type.
- **Error:** capability gap vs a real Language Server (generic substitution, parameter binding, return-type / JSDoc inference). Cheaper, lower-precision retrieval context for type-dependent reasoning.
- **Blocked by:** nothing structural; cost is the barrier — would mean running a real LSP subprocess (pylsp / tsserver / rust-analyzer) inside the indexer.
- **Phase:** long-term; relates to existing DEBT-005.
- **Notes:** carved at 8.14 planning per CLAUDE.md §11.3.


### DEBT-087 [LOW · Floating] — Python relative imports skipped by the extractor

- **Date:** 2026-07-01
- **Reproduce:** `_extract_python_imports` (`brain/memory.py`) drops any `from .mod import x` / `from . import y` — it emits absolute module paths only. TS/JS now resolve relative specifiers lexically into workspace paths, so Python module boundaries are under-represented in the dependency graph relative to TS/JS.
- **Error:** coverage asymmetry, not a defect. Historically justified when the worker had no project-root context; that context now exists (`req.file_path` + `req.workspace_root`, both plumbed in 8.14.0).
- **Resolution (unscheduled):** add relative-specifier resolution to the Python extractor, reusing the same lexical `posixpath` + workspace-guard approach as `_resolve_relative_specifier`, mapping a dotted relative import to a workspace path against the source file's directory.
- **Notes:** logged at 8.14.0 close per CLAUDE.md §11.3; marked in code as `TODO(DEBT-087)`.


### DEBT-088 [LOW · Floating] — `bfs_k_hop_backward` has the pre-8.14.1 resolved-form gap

- **Date:** 2026-07-01
- **Reproduce:** `bfs_k_hop_backward` (`core/memory/graphrag_extractor.py`, used by `TraceDataFlowTool`) seeds its `target_dependency IN (...)` query with the raw node string passed in. Since 8.14.0, a dependent references a changed file by import specifier (an extensionless TS/JS path or a dotted Python module), not the file's absolute path, so seeding the walker with a file path finds nothing and its multi-hop step re-feeds unresolved `source_file` values into the same mismatched query — the same under-counting gap `core/blast_radius.py` (8.14.1) was built to avoid.
- **Error:** architectural gap, not a defect — the walker predates the polyglot resolved-target concept.
- **Resolution (unscheduled):** migrate `TraceDataFlowTool`'s backward view onto the resolved-adjacency traversal introduced in `core/blast_radius.py`, or extend that module's reverse adjacency into a general-purpose resolved BFS both callers share.
- **Notes:** logged at 8.14.1 close per CLAUDE.md §11.3.


### DEBT-089 [LOW · Floating] — Blast-radius Python resolution is suffix-based, not sys.path-aware

- **Date:** 2026-07-01
- **Reproduce:** `core/blast_radius._build_python_suffix_index` maps every segment-aligned path suffix of an indexed `.py` file to that file, so a dotted Python import target resolves via suffix match rather than true `sys.path` resolution. Two indexed modules sharing a basename (e.g. `pkg_a/utils.py` and `pkg_b/utils.py`) both match a bare `import utils`, over-counting the blast radius.
- **Error:** declared MVP tradeoff, not a defect — over-counting is the safe direction for a pre-apply review gate (never silently under-count); the worker process has no view of the project's actual `sys.path` / installed-package resolution order.
- **Resolution (unscheduled):** resolve Python targets against the same import-root context a real interpreter would use (parsed `sys.path` entries, namespace packages) instead of a flat suffix index.
- **Notes:** logged at 8.14.1 close per CLAUDE.md §11.3.


### DEBT-090 [LOW · Floating] — Memory-snapshot export has no extension-side trigger

- **Date:** 2026-07-01
- **Reproduce:** the shared-memory export is fully wired on the backend — the `client_export_memory_snapshot` WS event, its `main.py` dispatch, and `core.memory_snapshot.export_memory_snapshot` — but no VS Code command / button sends that event, so a user cannot yet trigger an export from the UI. Import bootstrap already runs automatically at session init.
- **Error:** declared backend-only scope — 8.14.2's DoD is `mypy`/`pyright` only (no npm gate); the additive WS contract is forward-compatible, so wiring the FE later needs no backend change.
- **Resolution (unscheduled):** add an extension command-palette entry / status-panel button that emits `client_export_memory_snapshot` with the active `project_id` + `workspace_root`.
- **Notes:** logged at 8.14.2 close per CLAUDE.md §11.3.


### DEBT-091 [LOW · Floating] — Architecture digest omits git co-change coupling

- **Date:** 2026-07-02
- **Reproduce:** `architecture_digest` (`tools/perception_tools.py` + `brain/memory.build_architecture_digest_sync`) synthesizes languages/modules/hotspots/communities/entrypoints from the persisted graph, but carries no git co-change ("files that change together" — the reference graph's `FILE_CHANGES_WITH`) signal; there is no git-history substrate in the catalog to source it from.
- **Error:** declared scope cut — 8.14.5's manifest marks co-change "optional… where cheap"; with no existing git-log analysis it is not cheap, so it was omitted rather than built speculatively (§9 / §11.3).
- **Resolution (unscheduled):** add a bounded, idempotent git-log co-change extractor persisting pairwise change coupling, then surface a `co_change` section in the digest.
- **Notes:** logged at 8.14.5 close per CLAUDE.md §11.3.


### DEBT-092 [LOW · Floating] — Boundary graph cannot recover backend `server_*` emit edges

- **Date:** 2026-07-02
- **Reproduce:** `core.boundary_graph.refresh_boundary_graph` resolves WS/MCP boundary edges by matching the channel as a quoted string literal, but a backend `server_*` emit is a typed model construction (`api/websocket_manager.py` → `ServerStreamEndEvent(data=…)`) with no channel literal at the send site — so `trace_cross_boundary('server_stream_end')` returns `declares` + frontend `handles` but no core-side `emits`.
- **Error:** declared fidelity boundary — `declares`/`handles` are high-precision; `emits` is best-effort (only extension `client_*` object sends carry a literal).
- **Resolution (unscheduled):** a structural emit-site resolver that maps the `event_type` `Literal` back to the pydantic model class, then finds `send_personal_message(..., ModelClass(...))` constructions in the core emit path.
- **Notes:** advisory READ_ONLY tool — an empty emit list is never a "not emitted" verdict. Logged at 8.14.7 close.


### DEBT-093 [LOW · Floating] — Boundary graph has no auto-refresh on index-complete

- **Date:** 2026-07-02
- **Reproduce:** `TraceCrossBoundaryTool` builds the boundary graph on first query (empty-table trigger); after later code edits the stored edges can lag until an explicit `refresh_boundary_graph`. There is no `broadcast_indexing_complete` hook to rebuild.
- **Error:** declared scope cut — liveness is not required by the 8.14.7 DoD; the full rebuild is cheap (~71 channels) so a trigger is a small follow-on.
- **Resolution (unscheduled):** invoke `refresh_boundary_graph` (single-flight) from the index-complete path, or add a cheap staleness stamp the tool checks before serving.
- **Notes:** logged at 8.14.7 close.


### DEBT-095 [LOW · Floating] — Polyglot (TS/JS) runtime call-trace capture

- **Date:** 2026-07-02
- **Reproduce:** `core/call_trace_probe.py`'s tracer uses `sys.monitoring` (PEP 669), a CPython-specific facility — it can only observe Python `caller → callee` calls. The 8.14.8 dogfood PoC and any 8.14.8.1 substrate are Python-only; extension-side (TypeScript) runtime calls are never traced.
- **Error:** declared scope cut for the SPIKE — the manifest's dynamic-dispatch value case (`ToolDispatcher`) is Python, so the PoC's signal is representative for the backend, but the frontend gets no equivalent runtime confirmation.
- **Resolution (unscheduled):** a Node-side equivalent (`async_hooks` / V8 inspector protocol) feeding the same reconciler shape, or accept the Python-only scope permanently and document it as a hard boundary rather than a gap.
- **Notes:** logged at 8.14.8 close.


### DEBT-096 [LOW · Floating] — Sandbox/agentic-cell-integrated live trace capture

- **Date:** 2026-07-02
- **Reproduce:** the manifest's literal target was capturing traces "from the existing sandbox / agentic-cell execution" (a user project's run). 8.14.8 instead dogfoods AILIENANT's own pytest (declared deviation, `SCHEMA_EVOLUTION.MD`) because it is self-contained, real, and needs no target project or container work for the PoC.
- **Error:** the sandbox-execution capture path (tracing a user's own project as it runs inside `core/sandbox.py`'s tiers) remains unbuilt; the PoC only validates the *signal*, not that production capture path.
- **Resolution (unscheduled):** wire the same `CallTracer` into the trusted/native sandbox execution path (behind an explicit opt-in — tracing is not free and must never run unconditionally on user code) so production traces feed the persisted substrate instead of only the dogfood harness.
- **Notes:** logged at 8.14.8 close. `8.14.8.1` shipped the persisted substrate populated **out-of-band** (dogfood harness), so this live-capture path is still open.


### DEBT-101 [LOW · Floating] — Observed-call-edge substrate has no purge/TTL

- **Date:** 2026-07-02
- **Reproduce:** `core.db.persist_observed_edges` is append-only (`INSERT OR IGNORE`, never delete). When a file is reindexed and a symbol is renamed/deleted, its `observed_call_edges` rows are not purged — they accumulate as orphans over successive trace runs.
- **Error:** declared scope cut for 8.14.8.1 — capture is out-of-band, so no reindexer coupling was added. Correctness is preserved at the **read path**: `find_symbol_callers` only surfaces an added observed caller whose file is still in the catalog, so a stale row is never *shown*; it only wastes storage.
- **Resolution (unscheduled):** a reindex-coupled purge (mirroring `purge_symbol_definitions`) keyed by `caller_file`/`callee_file`, or a periodic TTL sweep, or a symbol-presence check at read to prune on access.
- **Notes:** logged at 8.14.8.1 close; append-only accumulation is deliberate ("never delete an observation"), so purge must be careful to drop only genuinely-orphaned rows.


### DEBT-103 [LOW · Floating] — Dart `package:` URI resolution is pubspec-unaware

- **Date:** 2026-07-03
- **Reproduce:** `_extract_dart_imports` (`brain/memory.py`) strips the `package:` prefix from `import 'package:foo/bar.dart';` and emits the remainder (`foo/bar.dart`) as the target, but never maps the leading `foo` package name back to the project's own directory layout (that mapping lives in `pubspec.yaml`, a file this extractor never reads).
- **Error:** declared scope cut for 8.14.11 — real resolution would require parsing a second file format entirely (YAML, plus the `name:`/`dependencies:` keys specifically), a materially larger feature than the source-AST extraction this round covers. A same-project `package:` import therefore rarely resolves past bare extraction today; it stays INFERRED, the same honest fallback as any unresolved import.
- **Resolution (unscheduled):** a small `pubspec.yaml` reader keyed by the project's own package `name:` field, feeding a Dart-specific candidate expansion (`package:<own_name>/x.dart` → `lib/x.dart`) alongside the existing relative-specifier path.
- **Notes:** logged at 8.14.11 close; `dart:` built-ins and relative (`'sibling.dart'`) specifiers are unaffected — only the same-project `package:` case is impacted.


### DEBT-111 [MEDIUM · Floating] — GraphRAG nebula limited to file/external node types

- **Date:** 2026-07-22
- **Reproduce:** open the Memory panel's Nebula. Nodes render as `file` (sphere) or `external-dep` (octahedron) only — there is no `function`/`class`/`module` shape, because the graph substrate is file-level.
- **Not blocked (re-analyzed 2026-07-22):** a full call-edge-materialized symbol graph would conflict with `docs/SCHEMA_EVOLUTION.MD` "Symbol-Level Call-Graph Substrate" (`observed_call_edges` is unbounded/append-only and would exceed `MAX_GRAPH_EDGES=5000` or fork the PPR/Leiden pipeline) — but a containment-only design is not: `symbol_definitions` already exists, is already populated per-project, and stores zero edges, exactly the Tier-2 catalog that decision already authorizes.
- **Enterprise target (Phase 11.2.S):** render `symbol_definitions` rows as satellite nodes of their owning file via a `defined_in` containment edge (bounded by symbol count, never enters `MAX_GRAPH_EDGES` accounting), extend the nebula's `nodeThreeObject`/shape map to the new `kind`s, and resolve "who calls this symbol" on-demand via the existing `find_symbol_callers` READ_ONLY tool (never bulk-loaded into the analytics graph). The rendering engine already scales; no decision amendment or new analytics pipeline needed.


### DEBT-113 [LOW · Floating] — Nebula picking + layout not yet scaled to 100k nodes

- **Date:** 2026-07-22
- **Reproduce:** the custom three.js engine renders via InstancedMesh (scales), but picking uses `raycaster.intersectObject` and the d3-force-3d layout runs one-shot on the main thread — both are comfortable at the current bounded node counts (≤5000) yet not the 100k design target.
- **Enterprise target:** GPU-picking (render instance-ids to an offscreen texture) for O(1) hit-testing at scale, and move the force layout into a Web Worker (the `nebula/engine` layout call and `nebula/picking` module are the seams). Built lazily so neither affects the current bundle.


### DEBT-114 [LOW · Floating] — Search pulse is not a real GraphRAG reasoning-path replay

- **Date:** 2026-07-22
- **Reproduce:** searching the nebula pulses matched nodes and their *incident* edges. It does not animate the actual path GraphRAG traversed to answer a query, because no retrieval trace is captured or emitted.
- **Enterprise target:** have the retrieval pipeline record the traversed node/edge sequence per query and surface it over the dashboard contract, then animate a pulse along that real path (the "reasoning made visible" the art direction envisioned).


### DEBT-137 [LOW · Floating] — Provider-native `cache_control` + cache telemetry not implemented

- **Date:** 2026-07-31
- **Reproduce:** run any planner/coder turn against Anthropic (or any provider) and inspect the
  request payload — no `cache_control` block is ever attached, regardless of prefix content.
  `core/token_ledger.py::TokenLedger.snapshot()` has no cache-read/cache-write fields.
- **File(s):** would touch `tools/llm_gateway.py::ainvoke`/`astream_byom`/`astream_byom_thinking`
  (application point, after `_inject_reasoning_scaffold` — ordering matters, see below); a new
  `tools/prompt_cache.py` (provider gate + minimum-token floor table); `core/token_ledger.py`
  (additive cache counters); the dashboard's `TelemetryPanel.tsx`.
- **Error:** capability gap, deliberately deferred — not overlooked. Manifest 12.1 originally asked
  for full provider caching premised on "the stable high-volume prefix (system prompt → tool/MCP
  schemas → GraphRAG context)." Measurement (see `docs/SCHEMA_EVOLUTION.MD` §41 and the 12.1 manifest
  spec) showed that premise doesn't hold today: the actual stable prefix is ~281-450 tokens
  (identity + role constraints + language mirror), below every current model's minimum-cacheable
  floor (512-4096 tokens depending on model). Two of the three named prefix components don't exist
  as stable content — tool/MCP schemas are absent from the coder's one-shot path entirely (blocked
  on DEBT-130), and GraphRAG context is assembled per-`target_file`, genuinely volatile per WBS step,
  not prefix. Applying `cache_control` to a sub-floor prefix would pay the 1.25× cache-write premium
  on every call for zero reads — a net loss, not a saving. 12.1 shipped the prerequisite instead
  (the HEAD/TAIL prompt split — §41) and deferred the rest here.
- **Blocked by:** re-evaluated at 12.7 close (DEBT-130/129/106 all landed) — the premise this entry
  hoped for did **not** materialize. DEBT-130's tool-grounding pre-pass is *conditional* (only fires
  when the step is thin on context) and its tool schemas live in a **separate reasoning call**
  (`core/tool_dispatch.py::make_gateway_reasoner`'s own hint message), never folded into
  `agents/coder.py`'s stable system-message HEAD (`agents/prompts.py::build_static_identity_prompt`).
  The coder's cacheable prefix is therefore unchanged by 12.7 — still ~281-450 tokens, still below
  every current provider's minimum-cacheable floor. Re-logged rather than closed.
  Secondary unblocker unchanged: bringing the chat path into scope — `core/task_service.py`'s
  `_MAX_HISTORY_MESSAGES = 24` conversation history is a genuinely growing multi-turn prefix (the
  textbook caching case) but was outside 12.1's stated scope.
- **Phase:** re-evaluate only if a future change folds tool/MCP schemas into the coder's stable HEAD
  prefix itself (not a per-turn reasoning call) — no such change is scheduled.
- **Notes:** even once unblocked, realistic savings on this codebase's BYOM-local-first deployment
  are modest — provider caching saves $0 on a local model, and on a cloud model the volatile
  per-step payload (file content, RAG snippets, mission context) dwarfs the cacheable prefix by
  roughly an order of magnitude. This is a genuine but small optimization, not a launch blocker.
- **12.10 decision (CLAUDE.md §4 Option B):** the Phase 12 closure gate originally required a
  "prompt caching tokens-saved metric > 0." Re-measuring at gate time reconfirmed this entry's own
  finding — the premise is still false — so the gate criterion was amended rather than this entry
  force-closed to satisfy it: 12.10 now certifies the cacheable-prefix *prerequisite*
  (`tests/test_prompt_prefix_stability.py`) instead, and this entry stays open with its existing
  trigger, unchanged.
- **External validation (2026-08-31 literature pass):** industry practice (Anthropic's own prompt-
  caching docs) confirms this entry's numbers independently — tool schemas count toward the cached
  prefix and invalidate it on any change, matching the DEBT-130 concern above; a sub-floor prefix
  pays the cache-write premium for zero reads, matching the "net loss" finding. One implementation
  constraint for the secondary unblocker (`core/task_service.py`'s `_MAX_HISTORY_MESSAGES = 24` chat
  history) was not yet on record: "Don't Break the Cache" (arXiv 2601.06007), a study of prompt
  caching under long-horizon agentic tasks, finds that summarizing or editing earlier turns
  invalidates the cache for everything after that edit — full-context caching only pays off when
  history is appended-to, never rewritten in place. If this unblocker is ever pursued, `_conversations`
  must stay append-only (new turns added at the tail, nothing upstream re-summarized) for the cache to
  hold; a compaction pass over old turns — which nothing in this codebase currently does to that
  dict — would defeat the exact prefix stability being sought. No action required today; recorded so
  the constraint is not rediscovered mid-implementation.


### DEBT-138 [MEDIUM · Blocked] — Agentic cell does not route through the devcontainer session tier

- **Date:** 2026-08-03
- **Reproduce:** `brain/agentic_cell.py::run_agentic_cell_node` resolves `core.sandbox.get_active_adapter()`
  (the locked oracle tier) unconditionally when opening its session — never
  `core.sandbox.resolve_execution_adapter(session_id=..., trusted=True)`, so a `requires_iteration`
  WBS step's ReAct loop never reaches the user's real devcontainer even after §43 (12.4) gave the
  trusted tier a working interactive-session implementation.
- **File(s):** `brain/agentic_cell.py:368-380` (the reroute); `core/sandbox.py::DevcontainerSandboxAdapter`
  (needs a `get_sync_surface()` override — the base class raises `NotImplementedError`, so the cell's
  `adapter.get_sync_surface(cwd)` call would fail the moment the reroute lands).
- **Error:** architectural precondition, not an oversight — deliberately scoped out of 12.4 (see the
  architect's review during that sub-phase's planning). Routing cell edits through a bind-mounted
  `SyncSurface` (the naive `get_sync_surface()` implementation — `core.workspace_sync.LocalFsSyncSurface`
  over the devcontainer's host workspace mount) would bypass the in-RAM VFS barrier and its
  base-hash stale-guard (`core/vfs_middleware.py`) entirely: a concurrent agent edit and a live user
  edit to the same file would silently corrupt the workspace, since neither side would ever see the
  other's write through the OCC guard that protects every other write path in this codebase.
- **Blocked by:** an OCC-safe sync surface — cell writes must reach the bind-mounted workspace only
  *through* the VFS barrier's stale-guard, never via a raw filesystem surface. No design is chosen yet;
  candidates include routing `SyncSurface.write_file` through the same `check_type_integrity`-adjacent
  hashing path `task_service.py`'s HITL apply uses, or restricting the cell to VFS-mediated reads/writes
  entirely and dropping direct `SyncSurface` access for this tier specifically.
- **Phase:** future OCC-safe sync-surface slice. Until then the §43 tunnel is reachable only via the
  `configurable["cell_adapter"]` test-injection seam the existing cell tests already use — test-reachable,
  not dead code, but not a production call path either.


### DEBT-139 [LOW · Floating] — Devcontainer session host driver has no real TTY

- **Date:** 2026-08-03
- **Detail:** `providers/devcontainerSessionHandler.ts` spawns `devcontainer exec ... -- /bin/sh` via
  plain piped stdio (`child_process.spawn`), not a pseudo-terminal — a deliberate MVP tradeoff (CLAUDE.md
  §11.2) made when §43 (12.4) rejected `node-pty`: a native module is a packaging/supply-chain cost the
  interactive session tunnel does not justify (CLAUDE.md §9), and the sentinel-marker command-boundary
  protocol (`core/command_boundary.py`, shared with the local `core.pty_session` PTY session) already
  gives command framing without a real TTY line discipline. Consequences: no job control (`fg`/`bg`/`Ctrl+Z`
  semantics), no `isatty()` — a program that branches on TTY presence (many REPLs, some build tools'
  progress-bar rendering) behaves as if piped, not interactive — and `signal: "interrupt"` sends a
  best-effort `SIGINT` to the child process directly rather than a true Ctrl-C to a foreground process
  group, so a child that spawns its own subprocesses may not propagate the interrupt to them.
- **Phase:** future terminal-fidelity slice, if usage shows this MVP tradeoff biting in practice
  (e.g. an operator running an interactive REPL or a TTY-sensitive build tool through the tunnel).


### DEBT-154 [LOW · Floating] — Apply-edge risk gate is still a command-pattern proxy, not a real edit-risk classifier

- **Date:** 2026-08-04
- **Reproduce:** N/A (design coarseness, not an error). The gate still decides "low-risk" by scanning
  an edit's added diff lines against `permissions.py::_RISK_PATTERNS` — a pattern set tuned for
  shell-command content, applied as a binary low/not-low proxy over code. Carried forward unchanged
  from DEBT-125, whose display-wiring half closed in 12.8.
- **File(s):** `ailienant-core/core/task_service.py`, `ailienant-core/core/permissions.py`.
- **Blocked by:** nothing — self-contained. A classifier (heuristic size/scope + secret/dep-graph
  signals, or a small model) returning a real low/medium/high verdict can replace the regex proxy.
- **Phase:** future safety slice.
- **Notes:** the conservative gate is already safe (fails toward the manual card); this is a
  precision gap, not a security gap.
- **Decision (2026-08-04, 12.14 — CLAUDE.md §4 Option C, Refactor):** explicit defer, reviewed rather
  than silently carried forward. Real defense-in-depth already exists via the regex gate plus the
  blast-radius check — the classifier's absence is a precision gap, not a missing safety layer. A
  semantic edit-risk classifier changes which edits apply silently, which makes it a dedicated
  safety-slice project on its own merits, not a pre-launch patch to bolt on. Not required for Phase 13.


### DEBT-155 [LOW · Floating] — File-read content preview not on the Glass-Box Timeline

- **Date:** 2026-08-04
- **Reproduce:** a `read`-kind marker shows a size metric (12.8) but never the file content itself —
  unlike a `command` node's stdout/stderr.
- **File(s):** would touch `core/vfs_middleware.py::make_safe_reader` or the coder's read call site.
- **Blocked by:** a token-hygiene/PII decision for how much of a read file's content is safe to
  surface in a detail box (charter §5.5) — file content is source code, not arbitrary shell output,
  and needs its own truncation/redaction design pass distinct from `record_exec`'s masking.
- **Phase:** future timeline-depth slice.
- **Notes:** carved out of DEBT-133 at its 12.8 resolution — the tool-call half of that entry is
  closed; this is the narrower remainder.


### DEBT-156 [LOW · Floating] — No automated CLA-assistant workflow

- **Date:** 2026-08-04
- **Reproduce:** N/A (a process gap, not an error). `CONTRIBUTING.md` §1 previously claimed an
  "automated CLA check posts a link" on a contributor's first PR — no such workflow exists in
  `.github/workflows/`. Corrected in 12.15 to describe only the manual `CLA.md` sign-off, which
  already works today.
- **File(s):** `CONTRIBUTING.md`.
- **Blocked by:** nothing technical — a real CLA Assistant needs a GitHub App / external service
  install, which has no operational justification for a solo pre-launch project.
- **Phase:** revisit once external contributors are actually onboarding (Phase 13+).
- **Notes:** deliberate MVP/patch decision per CLAUDE.md §11 — the manual path is the lower-friction,
  more honest fix today, and reversible the moment a second contributor shows up.


### DEBT-157 [MEDIUM · Floating] — No unit/integration/e2e taxonomy across the backend test suite

- **Date:** 2026-08-04
- **Reproduce:** `pytest --collect-only -q` in `ailienant-core` collects ~2,858 tests with no way to
  filter to a fast subset — no `pytest.ini`/`pyproject.toml` marker registration existed before
  12.16, and the only marks in active use are `anyio` (async plumbing, not a category),
  `parametrize`, and `skipif`.
- **File(s):** `ailienant-core/pytest.ini` (12.16, new — registers `unit`/`integration`/`e2e` markers
  for tests going forward only), `ailienant-core/tests/**` (237 existing test files, unclassified).
- **Blocked by:** nothing technical — retrofitting markers onto 237 existing files is a real,
  sizeable classification task (risk of silent mis-tagging if done in bulk without per-file review),
  deliberately out of scope for 12.16's registration-scaffold step.
- **Phase:** future test-taxonomy retrofit slice.
- **Notes:** even the strongest integration-style tests in the suite (the 50 `test_phase*_checkpoint_gate.py`
  files) mock the LLM/vector-store boundary per the project's own stated convention — "integration"
  in this codebase has never meant "against a live model."


### DEBT-158 [MEDIUM · Floating] — Playwright e2e coverage is a single 4-case Dashboard-only spec

- **Date:** 2026-08-04
- **Reproduce:** `ailienant-extension/e2e/` contains exactly one spec, `dashboard.spec.ts` (4 tests),
  scoped entirely to the Dashboard SPA. No chat/agent-turn flow, no VS Code extension-host e2e.
- **File(s):** `ailienant-extension/e2e/dashboard.spec.ts`, `ailienant-extension/playwright.config.ts`.
- **Blocked by:** nothing technical — the spec is genuinely real (spawns an actual backend subprocess
  via `e2e/run-backend.mjs`, drives real Chromium), just narrow in scope. Fixing it is a dedicated
  feature-sized effort, not a 12.x-sized patch.
- **Phase:** future e2e-breadth slice.
- **Notes:** 12.15 schedules this spec to run nightly in CI (`frontend-gate.yml`) — previously it ran
  in zero CI, nothing executed it automatically. Running-more-often does not close this entry;
  scope-breadth does.


### DEBT-169 [MEDIUM · Floating] — GraphRAG/tool retrieval has no reranking stage

- **Date:** 2026-08-17
- **Reproduce:** read `core/memory/semantic_memory.py::search_with_paths` / `search_snippets` and `core/tool_rag.py::select_tools`. Every retrieval path is a single `tbl.search(vector).metric("cosine").limit(k)` call against LanceDB, with the final ordering being raw cosine distance — `select_tools` literally sorts by `(_distance, name)` and stops. There is no cross-encoder second pass, no BM25/lexical fusion, no MMR diversification (a query can surface k near-duplicate chunks from the same file), and no reordering by recency: `search_with_paths` computes an `indexed_at` timestamp per result and it feeds `agents/recency.py::compute_recency_score`, which blends into the aggregate CSS *meter* — but the individual snippets/files handed to the LLM are never reordered by that signal, only the scalar sufficiency score is adjusted.
- **File(s):** `core/memory/semantic_memory.py` (`search_with_paths`, `search_snippets`, `_query_chunks`), `core/tool_rag.py` (`ToolRAGStore.select_tools`), `agents/recency.py` (`compute_recency_score` — the existing, unused-for-reordering recency signal to build on).
- **Error:** architecture/feature gap, not a correctness bug — single-stage kNN is cheap and works for small/medium corpora, but leaves precision on the table for larger codebases (near-duplicate retrieval, no lexical/semantic fusion, no diversity floor).
- **Blocked by:** nothing — a first cut (recency-weighted reorder of already-retrieved snippets, no new dependency) is a small follow-up; a real cross-encoder or MMR pass is a larger, separately-scoped change.
- **Phase:** future retrieval-quality slice.
- **Notes:** surfaced during a harness audit (2026-08-17). Cross-references **DEBT-149** (CSS's semantic term calibrated only against file-centroid distances, never chunk distances — same "retrieval precision left on the table" family) and **DEBT-140** (RESOLVED 12.13, added per-symbol chunking — the substrate a real reranker would sit on top of).


### DEBT-174 [LOW · Floating] — Coder-node edit generation never receives image attachments

- **Date:** 2026-08-17
- **Reproduce:** `agents/coder.py`'s edit-generation call (`acomplete_with_thinking`, not `LLMGateway.ainvoke`) never reads `state["attachments"]`. 13.0.1 (closing DEBT-168) wired attachments only into `agents/researcher.py`'s answer call — the node that owns comprehension of the request and seeds the plan everything downstream reasons from.
- **File(s):** `agents/coder.py` (the edit-generation call site), `tools/llm_gateway.py` (`LLMGateway.acomplete_with_thinking` — a separate method from the now-wired `ainvoke`, would need its own `images=` threading).
- **Error:** capability gap, not a correctness defect — an attached screenshot informs the plan via the researcher, but the coder generating a specific edit cannot see it directly (e.g. "match this exact color from the screenshot" would need a second look at the image mid-edit).
- **Blocked by:** nothing — same `_attach_images_to_messages` seam DEBT-168 built, applied to a second call site/method.
- **Phase:** future multimodal-payload slice.
- **Notes:** carved as an explicit scope boundary at 13.0.1 ship per CLAUDE.md §11.3, rather than widening that fix's blast radius to a second agent + a second gateway method.


### DEBT-175 [MEDIUM · Floating] — `TOOL_RAG_TOP_K` cannot rise until the Phase-5.7 gate's baseline is reworked; its prescribed remedy is near self-cancelling

- **Date:** 2026-08-18
- **Reproduce:** raise `TOOL_RAG_TOP_K` (`core/tool_rag.py`) from 5 to 8 and run `tests/test_phase5_7_checkpoint_gate.py::test_tool_rag_selection_yields_70pct_payload_reduction` — it fails at `reduction_ratio=0.471` against the 0.70 floor. That gate registers only 4 families (14 schemas, 6,302 chars), so selecting k of 14 caps the achievable reduction arithmetically. Measured against the *whole* 53-schema catalog (the R3 gate in `tests/test_phase8_8_tool_parity_gate.py`, baseline `store.all_schemas()`), the same k=8 sits at 0.8379 and the ceiling is k=13 — the two gates disagree because they measure different baselines, not because either is wrong.
- **Why the prescribed remedy does not work:** the gate's docstring directs the fix to "compress verbose `description=` strings — NOT to lower this threshold or shrink TOOL_RAG_TOP_K". Measured: description text is only **23.3%** of the payload (1,470 of 6,302 chars); the other 76.7% is structural JSON. Worse, `reduction_ratio = 1 - selected/eager` and `eager` is that same catalog, so compressing descriptions shrinks numerator and denominator together and the ratio barely moves. Deleting **100%** of description text — the absolute upper bound of the remedy — lifts the ceiling only from k≈3 to k≈4, and would degrade the embedding signal `select_tools` ranks on.
- **File(s):** `core/tool_rag.py` (`TOOL_RAG_TOP_K`, `TOOL_RAG_MIN_REDUCTION`), `tests/test_phase5_7_checkpoint_gate.py` (the 14-schema baseline), `tests/test_phase8_8_tool_parity_gate.py` (the full-catalog baseline).
- **Error:** not a correctness defect — a capability ceiling. The retrieval path is one tool narrower than it could be on small-context (local-tier) turns.
- **Workaround shipped (13.0.2):** the constant stays 5; callers needing N usable tools plus the `tool_search` hatch pass `k=N+1` themselves (`brain/agentic_cell.py`, `brain/nodes/subagent_worker_node.py`), so no path lost a usable tool. On any adequately-sized window the eager branch injects the whole role slice and the cap is never consulted at all.
- **Blocked by:** a decision the eager branch reframes — when the visible catalog fits the budget, injecting it whole at 0% reduction is the *correct* outcome, so a flat reduction floor measured on a subset baseline may be the wrong invariant now. Reworking a locked financial gate was deliberately out of scope for a change that had to pass it.
- **Phase:** future tool-catalog-economics slice.
- **Notes:** logged at 13.0.2 ship per CLAUDE.md §11.3, with the measurements above so the decision is made on numbers rather than the docstring's slogan.


### DEBT-176 [LOW · RESOLVED 2026-08-31, 8.20] — Tool-invocation telemetry: the emit-only half was already shipped

- **Date:** 2026-08-18 · **Resolved:** 2026-08-31 (8.20.7) — as a record correction, not new code.
- **Was:** recorded as "no tool-invocation telemetry exists", with the emit-only half marked worth doing.
- **Finding:** it already exists and is wired. `core/telemetry.py` carries the `tool_invocations` table and `log_tool_invocation`; `core/tool_dispatch.py::ToolDispatcher.dispatch` calls it from every outcome branch including DENY, behind a never-raise guard; `tests/test_telemetry.py` covers both the write and the uninitialized-DB no-op. The ledger row was stale — discovered by reading the code rather than trusting the entry, the same way DEBT-112 was.
- **Still rejected, unchanged:** consuming the data as a ranking prior in `select_tools`. It would make selection non-deterministic across runs, which checkpoint replay and Rewind depend on.
- **Files:** `core/telemetry.py`, `core/tool_dispatch.py`.

---

### DEBT-178 [LOW · Floating] — `toggle_plan_mode`'s READ_ONLY tier cannot express that it mutates the permission channel

- **Date:** 2026-08-18
- **Reproduce:** `tools/control_tools.py::TogglePlanModeTool` rewrites `state["session_permission_mode"]` — the channel `evaluate_action` consults to gate every dispatch — yet is registered `ToolPrivilegeTier.READ_ONLY` by deliberate design ("policy-neutral across the matrix", `control_tools.py:13-17`). `ToolPrivilegeTier` models effects on disk/network/processes; it has no vocabulary for "mutates the policy engine itself". 13.0.3 closed the concrete exposure (three dispatch-loop consumers could offer/call it while gaining nothing, since `core/tool_dispatch.py::_STATE_PROMOTERS` never promotes the write and `ToolDispatcher` pins its mode at construction) via a new `core/tool_registry.py::filter_loop_safe` predicate — but that predicate is opt-in per consumer. A future fourth dispatch-loop consumer that forgets to apply it reopens the same "tool that lies" defect.
- **File(s):** `tools/control_tools.py` (`TogglePlanModeTool`), `core/permissions.py` (`ToolPrivilegeTier`), `core/tool_registry.py` (`filter_loop_safe`, `_NO_AUTONOMOUS_LOOP`).
- **Error:** capability-model gap, not a live defect — 13.0.3 makes the current three consumers safe.
- **Blocked by:** a decision on the durable fix: (a) a new tier value the privilege matrix understands (e.g. `POLICY`), gated by `evaluate_action` like any other tier, so the exclusion is structural rather than a maintained list; or (b) move mode-toggling off the tool surface entirely (a dedicated orchestrator-only state-write path, since the orchestrator is its only real consumer and runs no dispatch loop). Both are bigger than a HITL-scoped fix and were out of scope for 13.0.3.
- **Phase:** future permission-model slice.
- **Notes:** logged at 13.0.3 ship per CLAUDE.md §11.3.


### DEBT-194 [LOW · Floating, PARTIALLY RESOLVED 2026-08-31] — No liveness signal exists to distinguish "local model is slow" from "local model is dead"

`ainvoke` makes one non-streaming `await litellm.acompletion(...)` — no incremental feedback exists until the full response or a timeout error returns, so DEBT-191's larger, hardware-scaled timeouts necessarily also mean a genuinely dead/hung local endpoint now takes proportionally longer to surface as an error (there is no way to tell the two apart with a single lump-sum request timeout). A proper fix would need `ainvoke` itself to move onto a streaming call, where each token's arrival could reset a per-chunk gap timeout instead of one whole-request timeout (a much closer proxy for "still working" vs. "hung") — real architectural work, correctly out of scope for DEBT-191's actual purpose.

**2026-08-31:** The exact gap this entry describes was closed for the paths that actually serve live chat generation — `astream_byom`/`astream_byom_thinking` (`tools/llm_gateway.py`) now wrap their `async for chunk in response` loop in `_iter_with_stall_detection`, an `asyncio.wait_for`-bound per-chunk idle timeout (`AILIENANT_LOCAL_STREAM_IDLE_TIMEOUT_S`, default 45s) that raises `LocalStreamStalledError` well before the full call-level timeout, independent of the client-side watchdog. `ainvoke`'s own single lump-sum timeout (the literal target of this entry's text) is unchanged — it remains the mini-judge/summarizer path, not the interactive chat turn, so this entry stays open for that narrower remaining scope rather than being marked fully RESOLVED.


### DEBT-199 [LOW · Floating] — `apply_patch`/`apply_commit` assume SWARM (`parallel_tasks`) stays dormant

`brain/apply_gate.py`'s per-step prepare/commit nodes were designed and tested against the RELAY (sequential) dispatch path only, matching `parallel_tasks` being hardcoded to `[]` at the two `agents/planner.py` call sites (`:291`, `:826`) — SWARM dispatch is planned but not live. Nothing in the gate itself enforces this; if SWARM is ever activated, two steps touching the same file could both reach `run_apply_commit_node` concurrently against the same `pending_base_hash`/`applied_files_log` state without any documented concurrency contract. Deliberately out of scope for 13.0.9 (SWARM activation is its own, unstarted body of work) — revisit before ever flipping `parallel_tasks` live.


### DEBT-200 [MEDIUM · Floating] — No one-click revert for an applied step; VS Code Local History is the only recovery path

13.0.9 fixed the dishonest "use Ctrl+Z to undo" claim (`core/task_service.py::_format_coding_summary` — no editor is ever focused, so Ctrl+Z had nothing to undo) by pointing the user at VS Code's own Local History (Timeline view) instead, which genuinely does capture every `applyEdit`+`save()` this project performs. That is a real, working recovery path, but it is manual and per-file. A proper fix would hold the pre-image already computed for every diff (`brain/apply_gate.py::_prepare_files` already builds a unified diff against the pre-edit content) in a short-lived blob store keyed by `patch_id`, and offer a one-click "revert this step" action from the same card the diff/checklist already render. Not built in 13.0.9 — declared explicitly as an MVP compromise in the approved plan, not an oversight.


### DEBT-107 [MEDIUM · Floating] — Autonomous LLM-driven `DispatchPlan` emission is deferred

- **Date:** 2026-07-04 (8.15 planning)
- **Reproduce:** `brain/dispatch_emitter.py` ships the full mechanism for fanning a turn out into subagent dispatch — an injected `dispatch_plan_fn` hook, an `AILIENANT_DISPATCH_DEBUG` synthetic path, counter resets, and the graph wiring that consumes a `DispatchPlan` once produced — but the production model prompt that decides WHEN and HOW to fan out was never built. Emission fires only via the seam/hook or a directly-seeded `dispatch_plan`, never from the model's own judgment.
- **File(s):** `ailienant-core/brain/dispatch_emitter.py`.
- **Error:** capability gap — the mechanism is complete and tested, but nothing populates it autonomously in production.
- **Phase:** future dispatch-emission slice.
- **Notes:**
### DEBT-115 [LOW · Floating] — Per-project token-cost bucketing deferred from 11.1

- **Date:** 2026-07-24 (11.4 planning)
- **Reproduce:** `token_ledger.snapshot()` (`main.py`, exposed via `/api/v1/telemetry/tokens`) is a process-global in-memory aggregate with no `project_id` dimension, so the dashboard's cost cards stay global — honestly badged as such, not silently wrong. A per-project view needs the FinOps ledger to bucket accrual by project (ephemeral across restart unless also persisted).
- **File(s):** `ailienant-core/main.py`, `ailienant-core/gateway/ledger.py`.
- **Error:** capability gap — the global aggregate is correct as far as it goes; per-project bucketing was never built.
- **Phase:** future FinOps slice.
- **Notes:**
### DEBT-135 [LOW · Floating] — Playwright dashboard fixture bypasses the real indexer

- **Date:** 2026-07-27 (11.9)
- **Reproduce:** `ailienant-core/tests/e2e/seed_dashboard_fixture.py` writes directly into the catalog SQLite + LanceDB stores via existing low-level helpers (`upsert_indexed_file`/`upsert_dependencies`/`SemanticMemoryManager._write_record`) to seed fixture data fast — it proves the dashboard's READ side renders correctly, but never exercises the indexer→dashboard pipeline end-to-end with a real crawl.
- **File(s):** `ailienant-core/tests/e2e/seed_dashboard_fixture.py`.
- **Error:** test-fidelity gap — the covered surface (dashboard rendering) is real; the uncovered surface (indexer-to-dashboard integration) is a genuine hole, not a false-positive risk in what IS tested.
- **Phase:** future e2e-fidelity slice.
- **Notes:**
### DEBT-136 [LOW · Floating] — Playwright suite is Chromium-only, no cross-browser matrix

- **Date:** 2026-07-27 (11.9)
- **Reproduce:** `ailienant-extension/playwright.config.ts` configures a single Chromium project — no Firefox or WebKit run of `e2e/dashboard.spec.ts`. Accepted as smoke-gate scope for a locally-served SPA (the dashboard is never rendered inside an actual browser the user chooses; it is always the VS Code webview's own Chromium-based renderer).
- **File(s):** `ailienant-extension/playwright.config.ts`.
- **Error:** test-coverage gap, not a defect — the untested browsers are not part of this product's real runtime surface.
- **Phase:** future cross-browser slice.
- **Notes:**
### DEBT-148 [LOW · Floating] — Dashboard vector scatter map surfaces only file-level embeddings

- **Date:** 2026-08-03 (12.13)
- **Reproduce:** `/api/v1/memory/vectors` (`pca_project_2d`) projects only the file-level embedding per file. 12.13 added per-symbol chunk vectors (`symbol_chunk_embeddings`) for files over the chunking threshold, but the scatter map has no visualization for them — `VectorPoint` would need to become multi-valued per file to show chunk-level points alongside (or instead of) the file centroid.
- **File(s):** `ailienant-core/api/memory_dashboard.py` (`pca_project_2d`, `VectorPoint`).
- **Error:** feature gap — the existing file-level scatter map is correct as far as it goes; chunk-level visualization was never built.
- **Phase:** future dashboard chunk-visibility slice.
- **Notes:**
---

## Closed Entries

*(Entries here are compact summaries. Full resolution notes are in git history and in the entry's Resolution block before it was moved here.)*

- **DEBT-043 — Orchestrator tools unbound to live state** — **RESOLVED 2026-06-15** (8.10.2). Added `make_get_wbs_status_tool` / `make_emit_hitl_request_tool` + `build_orchestrator_tools(state)` in `tools/agent_tools.py` — the canonical path that constructs the audited tools bound to the live graph state. The deterministic node's flag contract is left intact (§10); invocation moves to [DEBT-066].
- **DEBT-046 — Coder EXECUTE wrappers lack the interactive HITL card** — **RESOLVED 2026-06-15** (8.10.2). New `_gated_exec` + `_GatedExecTool` base + `make_coder_execute_tools(state)` thread `session_id`/`session_permission_mode` so EXECUTE-tier commands route through `evaluate_action` → `request_human_approval` (mirrors the MCP gate), honoring the trust-once valve; `guard_env_file` excluded (own gate). Additive — unfactoried construction unchanged.
- **DEBT-042 — Analyst search_fn unwired** — **RESOLVED 2026-06-15** (8.10.2). `tools/mcp_adapter.py::make_brave_search_fn()` resolves the brave-search session lazily and is resilience-wrapped (`wait_for` + broad except → degradation string, never raises); `make_web_search_tool` / `make_dependency_audit_tool` inject it by default. CVE/web search go live the moment the session connects.
- **DEBT-028 (hooks half) — Hooks persisted but never executed** — **RESOLVED 2026-06-15** (8.10.2). `TaskService._run_patch_hooks` runs enabled `pre_patch`/`post_patch` commands through the sandbox adapter around the single `apply_patch_set` commit; the ceiling is delegated to the adapter's `timeout_s` (kills+reaps — no outer `wait_for` orphan). `pre_patch` non-zero/timeout/no-adapter fails-closed (vetoes); `post_patch` is advisory; every fault is non-fatal + logged. (Skills half closed earlier in 8.4.5.)
- **DEBT-034 — Gateway `project_id` hashing is path-format-fragile** — **RESOLVED 2026-06-15** (8.10.1). `project_id_for` (core/storage_paths.py) now hashes `os.path.normcase(os.path.normpath(workspace_root))`; `PathResolver.computeProjectId` mirrors it byte-for-byte via Node `path.win32/posix.normalize` + a trailing-separator strip that preserves the disk/UNC/POSIX root (a naive regex strip would corrupt `C:\`→`C:`). One-time lazy re-index on next workspace open.
- **DEBT-038 — Production benchmark service imports the test tree** — **RESOLVED 2026-06-15** (8.10.1). Relocated the 11 harness modules (+ `corpus/` and `datasets/` fixtures) from `tests/benchmark/` to a shippable `core/benchmark/` package; repointed all `tests.benchmark.*` imports to `core.benchmark.*` (harness, 7 test files, `benchmark_service.py`, `test_gateway_eval_surface.py`). Reverse-dependency guard: zero `from tests` imports remain under `core/benchmark/`. `report.schema.json` stays in `tests/benchmark/` (read via the test's own `__file__`).
- **DEBT-040 — `tool_search` role resolution stale across per-step transitions** — **RESOLVED 2026-06-15** (8.10.1) via Explicit State Augmentation. Root cause: the router never re-set `active_role` per step — it inherited the task-initial value. The `Send` payload now carries `active_role = step.target_role` (engine.py, both SWARM and RELAY sites), so the wired tool-selection path is per-step-correct; `_resolve_active_role` is config-first and the ambient `_task_active_role` ContextVar was removed entirely (def + task_service set/reset), eliminating staleness and cross-WS leakage. Residual: the agent-callable `tool_search` dispatch itself is still unwired (the DEBT-043/046/054 cluster) — this makes selection correct now and resolution correct when that dispatch lands.
- **DEBT-064 — Agent organizes its own runtime files → OCC stale-apply** — **RESOLVED 2026-06-14** (8.10.x). The telemetry log isn't a code file, so it reached the agent via the workspace tree (`_build_tree`, a raw `os.walk` that lists hidden files); the "move" was a patch through `apply_patch_set`. Fixed at the source (filter the tree) + a write-layer guard dropping internal paths from the patch set + a VFS read-block on `.ailienant_telemetry.log*`. `is_ailienant_internal_path` (core/storage_paths.py) exempts the user-authored `.ailienant/AILIENANT.md`.
- **DEBT-063 — Plan executes out of WBS order** — **RESOLVED 2026-06-14** (8.10.x). WBS steps carry only implicit `step_number` ordering (no dependency DAG), so the `tci>80` blanket SWARM fan-out (`planner.parallel_tasks`) ran dependent steps out of order. Set `parallel_tasks=[]` → always sequential RELAY; SWARM dispatch left dormant for a future explicit-dependency DAG.
- **DEBT-065 — Auto-mode summary wording** — **RESOLVED 2026-06-14** (8.10.x). `_format_coding_summary` took only `plan_surface`; added a backward-compatible `auto_apply` branch so Auto reads "Applying N file change(s) directly…" instead of "review the diff and authorize."
- **DEBT-055 — Chat scroll regression** — **RESOLVED 2026-06-14** (8.10.0). The real defect was the Natt/Analyst pane: `.ws-natt-body` is a `1fr` grid track missing `min-height: 0`. The main chat list was already correct.
- **DEBT-056 — Text HUD fixed height (no auto-resize)** — **RESOLVED 2026-06-14** (8.10.0). Shared `useAutoResizeTextarea` (`useLayoutEffect`) hook on PromptBar + NattPromptBar; bounds in CSS (`min-height: 2.5rem; max-height: 12rem`). Introduced a HUD height regression, fixed under DEBT-062.
- **DEBT-060 — Diff-authorize card duplicated on tab switch with no diff** — **RESOLVED 2026-06-14** (8.10.0). `server_plan_document` re-injected its summary on every panel reveal; made the webview handler idempotent by summary content + a content-based host re-post guard. (Renumbered from a collision with the existing DEBT-057.)
- **DEBT-061 — Pipeline execution trace collapsed to a 1px box line** — **RESOLVED 2026-06-14** (8.10.0). Redesigned `.ws-thinking` from a bordered widget into an inline borderless trace. (Renumbered from a collision with the existing DEBT-058.)
- **DEBT-062 — Telemetry HUD height regression + context-window indicator** — **RESOLVED 2026-06-14** (8.10.0). Shared `--hud-rest-height` aligns composer + telemetry card; merged OCC ring and context meter into one split donut (`OccContextRing`); per-model window resolved via litellm `get_model_info`; apply-result paths backtick-wrapped. Live used-tokens read may still need a runtime trace (diagnostic logged).
- **DEBT-001 — tools.patch_tool: LangChain @tool decorator stub mismatch** — **CLOSED 2026-06-05** (Phase 8.0.1). Removed stale `# type: ignore[misc]` on `tools/patch_tool.py:219` after langchain-core stubs caught up. `mypy --strict tools/patch_tool.py` → 0.

- **DEBT-002 — agents/contract_guard.py: MODEL_MEDIUM not explicitly exported** — **CLOSED 2026-06-13** (verified post-Phase 8.0.2). `mypy --strict agents/contract_guard.py` → 0. The attr-defined error was resolved when `contract_guard.py:100` was changed to import `MODEL_MEDIUM` from `shared.config` directly (same fix as DEBT-015).

- **DEBT-003 — brain/swarms.py: BaseCheckpointSaver missing type args** — **CLOSED 2026-06-05** (Phase 8.0.0). `Optional[BaseCheckpointSaver]` → `Optional[BaseCheckpointSaver[Any]]` in `brain/swarms.py:189`.

- **DEBT-004 — brain/swarms.py: stale unused-ignore comments** — **CLOSED 2026-06-05** (Phase 8.0.0). Removed 8 stale `type: ignore` comments; 4 minimal targeted ignores retained for DEBT-014.

- **DEBT-006 — Inline diff / chat code had no syntax highlighting (shiki deferred)** — **CLOSED 2026-06-05** by Phase 7.16 (host-delegated tokenization). Engine moved to the host; webview paints scope-colored spans with `--vscode-*` CSS vars and zero grammar deps; `dist/workspace.js` 548.2 KB < 550 KB ceiling. Verified by the 7.16.3 checkpoint gate (10/10) + a permanent esbuild ceiling guard. Spawned DEBT-012 (`disableWordDiff` trade-off, still open).

- **DEBT-008 — Coding turns stream node-level narration, not LLM tokens** — **CLOSED 2026-06-05** (Phase 7.17.0-B / ADR-739). Thinking tokens stream via `acomplete_with_thinking` to Thought Box. Structured JSON answer buffers by design; residual tracked as DEBT-013.

- **DEBT-009 — MCTS variant-search is offline-only** — **CLOSED 2026-06-09** (Phase 7.19.2 / ADR-749). MCTS wired into the ReAct agentic cell (`brain/agentic_cell.py`) for multi-candidate fix paths; linear spine stays MCTS-free (MCTS-DEFER gate row). Multi-axis governor added in 7.19.3 (ADR-750).

- **DEBT-015 — agents/contract_guard.py: MODEL_MEDIUM import** — **CLOSED 2026-06-05** (Phase 8.0.2). Import redirected from `tools.llm_gateway` to `shared.config`. `mypy --strict agents/contract_guard.py` → 0.

- **DEBT-016 — brain/summarizer.py: strict-mode type-arg** — **CLOSED 2026-06-05** (Phase 8.0.2). `run_summarize_node` typed as `(state: Dict[str, Any]) -> Dict[str, Any]`. `mypy --strict brain/summarizer.py` → 0.

- **DEBT-018 — brain/memory.py: networkx GraphRAG has no memory bound** — **RESOLVED 2026-06-08** (Phase 8.1.B). `MAX_GRAPH_EDGES = 5000` cap-and-skip guard; deterministic `finally` teardown on both PPR builders. Regression: `test_oversized_graph_is_skipped_gracefully` + `test_at_cap_boundary_still_computes`.

- **DEBT-019 — api/websocket_manager.py: async request-buffer leak** — **RESOLVED 2026-06-08** (Phase 8.1.A). Guard-at-store drops late orphan responses; `disconnect()` wakes suspended waiters in O(1). Regression: `tests/test_ws_buffer_lifecycle.py` (6 cases).

- **DEBT-020 — tree-sitter stubs incomplete (6 × attr-defined, 1 × union-attr)** — **RESOLVED 2026-06-08** (Phase 8.1.C). `node: Any` / `tree: Any` retyping in `brain/prompt_builder.py`; local-variable narrowing guard in `brain/memory.py`. 7 ignores eliminated.

- **DEBT-021 — core/io_coalescer.py: bare Callable missing type parameters** — **RESOLVED 2026-06-08** (Phase 8.1.D). `Optional[Callable]` → `Optional[Callable[..., Any]]`; `asyncio.Task` → `asyncio.Task[None]`. 5 `type-arg` errors eliminated.

- **DEBT-022 — api/websocket_manager.py: 4 × arg-type on enum literals** — **RESOLVED 2026-06-08** (Phase 8.1.E). 4 broadcast method params narrowed from `str` to `Literal[...]` types; one cascading caller required `cast(Literal["success","error"], ...)`.

- **DEBT-023 — Miscellaneous single-site strict suppressions (5 ignores)** — **RESOLVED 2026-06-08** (Phase 8.1.F). `_require_token` typed; `DirtyBuffer` cast; `tup.checkpoint` cast; `Resolution` cast; `on_thinking` None guard. All 5 ignores eliminated.

- **DEBT-026 — MCP-discovered tools hardcoded to READ_ONLY (privilege fail-open)** — **RESOLVED 2026-06-10** (Phase 8.4.1). `classify_tool_privilege()` added to `core/permissions.py` (catalog > verb heuristic > DANGEROUS fail-closed). Dispatch-time trust-once valve tracked as DEBT-029 (also now resolved).

- **DEBT-029 — MCP tool dispatch consults no permission guard + no trust-once valve** — **RESOLVED 2026-06-11** (Phase 8.4.7). `McpToolAdapter._arun` resolves session context from ambient ContextVars; `_session_trust` dict per `(session_id, tool_name)`; FE `request_kind="MCP_TOOL_CALL"` card. 15 dispatch-guard tests green.

- **DEBT-030 — BYOM dashboard: no Google preset + `_ensure_v1` mangles native cloud endpoints** — **RESOLVED 2026-06-10** (Phase 8.4.8 + 8.4.9). `core/config/provider_registry.py` single source of truth for 12+ providers. Re-test 404 and OpenRouter double-`/v1` fixed in 8.4.9. `tested_models` cached per-endpoint. 20 new tests green.

- **DEBT-031 — MCP secret-value store + connect-time env injection** — **RESOLVED 2026-06-11** (Phase 8.4.6, load-bearing half). `core/config/mcp_secrets.py` backend-masked secret store (`0600`, atomic); env injection via `_build_stdio_params` at connect time. Config portability remainder tracked as DEBT-033.

---

## Appendix: Reproduction Quick-Reference

```powershell
# Run from: C:\Proyectos\Proyect_Ailienant\ailienant-core

# DEBT-005 (exploratory — count changes over time; 4 errors confirmed 2026-06-13)
.\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l

# DEBT-011 (pre-existing red gate test — heap baseline ceiling)
.\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q
```
