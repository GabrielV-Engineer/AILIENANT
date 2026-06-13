# AILIENANT: Autonomous Agent Operational Charter

You are an autonomous contributor to **Project AILIENANT**. Your prime directive is to maintain architectural integrity and prevent technical debt. This charter is binding. It is structured to self-scale: it anchors to the project's living roadmap rather than to any frozen phase number, and it codifies the runtime-safety invariants the codebase already exemplifies so they cannot be regressed. Violation of any protocol results in a rejected mutation.

---

## 1. Strategic Context & Source of Truth

- **Mandatory Context Anchoring:** Before proposing or executing any mutation, you MUST read and analyze `docs/PROJECT_MANIFEST.md`, `DEVELOPERS.md`, and `docs/DEV_JOURNAL.md` to understand the full roadmap and the current phase status.
- **Supporting Docs:** Cross-reference `docs/SCHEMA_EVOLUTION.MD` for state definitions and `docs/SYSTEM_PROMPTS.md` for agent logic before modifying core backend/frontend boundaries.
- **Dynamic Blueprint Anchor (self-scaling):** Never assume a specific phase number; resolve it from the manifest at task time.
  1. Determine the **currently active phase** from `docs/PROJECT_MANIFEST.md` — the first sub-phase not marked `[x]`.
  2. If that phase has a blueprint in `docs/` (named `PHASE_<N>_BLUEPRINT.md`), that blueprint is **binding** and MUST be read **before** any task touching its scope — including read-only research, design questions, and code mutations.
  3. Any deviation from the active blueprint requires a blueprint amendment in the same change. When a phase closes, the anchor advances automatically to the next open phase; do not hardcode a phase into this document or into code.

---

## 2. Technical Stack & Commands

- **Backend (Python):** Located in `/ailienant-core`. Use the local `venv` interpreter and `pytest`.
- **Frontend (TypeScript / VS Code):** Located in `/ailienant-extension`. Use `npm` for builds and linting.
- **Cross-platform note:** The primary environment is Windows / PowerShell, but code and commands must remain portable to POSIX CI. Prefer tooling that runs identically on both; see 5.6.

| Zone | Verification gate |
|------|-------------------|
| Backend type/lint | `npx pyright` and `mypy .` (catches Pylance UI errors at the CLI) |
| Backend tests | `pytest` |
| Frontend build | `npm run compile` |
| Frontend lint | `npm run lint` |

---

## 3. Domain Paradigms (Impact-Zone Architectural Stances)

Code is not uniform; the required engineering stance depends on the blast radius of the zone being touched. Identify the zone first, then apply its stance.

- **Gateway / MCP / Transport** (`gateway/`, `transport/`, `tools/mcp_adapter.py`) — the untrusted boundary. Treat every inbound payload as hostile: strict schema validation at the edge, explicit timeouts on every external call, single-flight admission and idempotency, and ledger atomicity (reserve, then commit, then refund-on-failure). A caller fault must never crash the host process.
- **Core / Eval / Brain** (`core/`, `brain/`, the eval surface) — the deterministic engine. Demand pure determinism and immutability: frozen specifications stay frozen, no hidden global mutation across an `await`, memory efficiency, and token hygiene before any I/O enters semantic memory.
- **Frontend / VS Code Host** (`ailienant-extension/src/`) — the reactive layer. Demand reactivity and explicit UI-state ownership; isolate long or heavy work from the extension host process; never block the event loop.

---

## 4. Architectural Foresight & Conflict Detection

You act as a **Strategic Auditor**. Before applying any change:

1. **Roadmap Impact Analysis:** Evaluate whether the modification clashes with future phases defined in `docs/PROJECT_MANIFEST.md`.
2. **Conflict Notification:** If fulfilling a task risks blocking a future milestone or violates a `docs/SCHEMA_EVOLUTION.MD` contract, you **MUST STOP** and notify the user.
3. **Resolution Protocols:** Present these options:
   - **Option A (Pivot):** Propose an alternative implementation that satisfies both the current task and future scalability.
   - **Option B (Manifest Update):** Suggest specific modifications to the WBS in `docs/PROJECT_MANIFEST.md` to accommodate the new architectural reality.
   - **Option C (Refactor):** Identify existing technical debt that must be cleared before proceeding.

---

## 5. Engineering Invariants (Runtime Safety)

These invariants govern the application's runtime behavior, not just your edits. They are already exemplified in the codebase's best paths; mandating them prevents regression.

1. **Async Lifecycle & State Cleanup:** Every concurrent in-memory state mutation MUST have a guaranteed cleanup path — either a `finally` block or a closure / done-callback that always fires. The canonical pattern is the single-flight counter in `core/benchmark_service.py`, whose reserved slot is released by the caller's done-callback. No critical section may introduce an `await` that breaks its atomicity.
2. **No Exception Hiding:** A bare or broad `except` is forbidden unless it either (a) re-raises the original error, or (b) logs the root cause with `exc_info=True` and carries an explicit justification comment (`# noqa: BLE001`), exactly as `core/benchmark_service.py` does where a fault must not crash the host. Silently swallowing an error is a rejected mutation.
3. **Idempotency by Default:** Any tool or endpoint that mutates state (charging the ledger, writing a file, committing a checkpoint) MUST be safe under accidental retries — LLM ReAct loops, MCP clients, and network timeouts all re-issue calls. Enforce safety via single-flight admission, `content_hash`, or `request_id` deduplication (see `tests/test_single_flight.py` and the indexer / task-service dedup paths). A non-idempotent mutation that an LLM can retry is a correctness defect.
4. **Transactional Side Effects:** A function combining multiple side effects (budget, disk, and state, for example) MUST prepare everything before committing anything, or provide explicit compensation — the reserve/refund pattern in `gateway/ledger.py`. No code path may leave the system half-committed after a mid-operation failure.
5. **Token Hygiene & Defensive Pagination:** Never inject unbounded raw I/O — large files, log dumps, full table scans — into a graph node, an agent's context, or an MCP client. Truncate, paginate, or vector-digest first (the GraphRAG digest is the reference). Returning massive raw payloads exhausts the context window, raises cost, and degrades model attention.
6. **Cross-Platform Safety:** Assume both Windows (NTFS) and POSIX run the code. Use `pathlib` for all path handling; explicitly close temporary file handles before calling `os.replace` (Windows raises `PermissionError` on a still-open handle); prefer atomic `os.replace` over a manual move; never use `os.system` or raw shell for logic that must be portable.

---

## 6. Security Posture (Zero-Trust)

1. **Zero-Trust Filesystem:** Always use `ls` and `grep` to build a mental AST of the files before editing. Never edit blind.
2. **Zero-Trust Inputs:** Whenever you touch an input or file-I/O endpoint, proactively check for Path Traversal / LFI and Time-of-Check-to-Time-of-Use (TOC-TOU) races. Normalize and confine every resolved path to its sandbox root before use.
3. **Secrets Hygiene:** Never log, echo, embed in a prompt, or commit API or BYOM keys. Use `key_ref` indirection (see `api/byom.py`) so raw secrets never round-trip through state or transcripts. Treat `.env` files and credentials as untracked by default.

---

## 7. Mutation Protocols

- **Zero-Trust Execution:** Build the mental AST (Section 6.1) before any edit.
- **Granular Edits:** Prefer targeted edits over full-file rewrites to preserve existing comments and formatting.
- **Definition of Done (DoD):** A task is complete only when the verification commands for its zone (Section 2) return **Exit Code 0**.
- **No Global Installs:** Never install packages globally. Always use the project's local environments.

---

## 8. Verification & Testing Strategy

1. **Zero-Degradation (Stop the bleeding):** After modifying any Python code, you MUST run `npx pyright` and `mypy .`. Your specific changes MUST NOT introduce a single new type error, syntax issue, or Pylance warning. `mypy .` is the enforced gate — not `mypy --strict`, which drags transitive debt unrelated to your change.
2. **The Boy Scout Rule (Gradual Healing):** If a file you modify already contains Pylance/Pyright or syntax errors, opportunistically fix them alongside your change. Leave the file cleaner than you found it.
3. **Test Taxonomy (mock vs. integration):** Use hermetic stubs/mocks to seal off a heavy engine at a boundary (the Gateway pattern, which avoids spinning the full cognitive engine in a unit test). Use real integration tests for contract-critical paths where a stub would hide a real break. Phase gates follow the sibling convention — a `test_phaseX_checkpoint_gate.py` living next to the phase's code, test-only, asserting the phase's invariants.

---

## 9. Dependency & Supply-Chain Governance

- A new third-party dependency requires explicit justification and the lightest viable option that satisfies the need. Pin versions.
- Never install packages globally (reaffirms Section 7).
- Standing precedent: `scipy` was rejected in favor of a hand-rolled `degree_centrality`. Prefer a small, auditable implementation over a heavyweight transitive dependency tree whenever the surface area is narrow.

---

## 10. Contract & Schema Evolution

- Wire contracts — structured-JSON envelopes, MCP tool schemas, WebSocket message types, persisted state — are **additive-only** and **version-tagged**. Never break or silently repurpose an existing field.
- Consumers must tolerate unknown/missing fields (context-tolerant version tagging). When a contract must change shape, add a new versioned variant rather than mutating the old one.
- Cross-reference `docs/SCHEMA_EVOLUTION.MD` for the authoritative state definitions before any contract change.

---

## 11. Enterprise Standard & Technical-Debt Management

Every change defaults to an uncompromising Enterprise-grade standard. If a tactical tradeoff is unavoidable, manage it strictly:

1. **Enterprise Default:** Prioritize scalable, robust, Enterprise-grade solutions for every creation or modification. Do not compromise architecture or performance unless strictly constrained by the current environment.
2. **MVP / Patch Declaration:** If you implement an MVP, a localized patch, or a workaround designed to contain blast radius or respect a current constraint, you MUST explicitly declare this tradeoff in your response.
3. **Deferred Refactoring Phase:** Every MVP or temporary patch MUST be logged immediately as a future phase or sub-phase (in `docs/TECH_DEBT_BACKLOG.md` or the WBS) dedicated to its Enterprise refactor. Zero untracked technical debt is allowed.

---

## 12. Observability Standards

- Failures are logged with their root cause and `exc_info=True`, never swallowed (reinforces Section 5.2).
- Use explicit, appropriate log levels. Do not use bare `print()` in runtime paths — route through the module logger.
- A log line should let a future reader reconstruct what happened without re-running the code.

---

## 13. Code Comments & Documentation Policy

Every time you modify or create code:

1. **Reference Eradication:** When you encounter comments or strings referencing a specific phase, sub-phase, ADR, or blueprint, actively delete or scrub those project-management references to keep the codebase clean.
2. **Zero-Reference Creation:** Do not mention any phase, sub-phase, or blueprint in new comments or strings. Code documentation must be timeless — explain the "why" and "how" of the implementation, deliberately omitting historical project-management metadata.
3. **Strict English Standardization:** The codebase maintains a single language. Translate any Spanish comments, docstrings, or string literals you encounter into professional technical English. All newly created documentation and strings MUST be written exclusively in English.

---

## 14. Phase Closure & Git Protocol (Manual Execution)

When you complete a phase or sub-phase defined in `docs/PROJECT_MANIFEST.md`, perform these documentation updates before declaring completion:

1. **`docs/PROJECT_MANIFEST.md`:** Flip the task checkbox from `[ ]` to `[x]`. Add nothing else — no date, no outcome summary, no Status block. Completion detail belongs exclusively in `docs/DEV_JOURNAL.md`.
2. **`docs/DEV_JOURNAL.md`:** Append exactly one entry using the strict template below.
3. **`DEVELOPERS.md`:** Reflect any global system changes. If your implementation created a new file or structural directory, you MUST find and update the **"Repository Layout"** section in `DEVELOPERS.md` to keep the structural tree accurate.

### DEV_JOURNAL entry template (strict — do not exceed ~12 lines)

```
## [Phase.subphase]: [Short title] — YYYY-MM-DD
**Status:** COMPLETE | **Gates:** mypy 0/N · pytest N passed [· pyright 0 · npm compile 0]
- Shipped: [one sentence describing what was delivered]
- Key decision: [one sentence — only if architecturally non-obvious; omit line otherwise]
- Deferred: DEBT-N — [one sentence] (omit line if none)
```

Prohibited in journal entries: F-numbered audit findings, "Contexto" sections, multi-bullet implementation narratives, file tables (git log is the authoritative file record), architectural decision records (those go in the blueprint or `docs/SCHEMA_EVOLUTION.MD`).

### PROJECT_MANIFEST new task item template

```
- [ ] **N.M. Task Title**
  [Spec: what this builds, DoD, constraints, target files — the active contract.]
```

### Interaction protocol (what goes where)

| Event | `PROJECT_MANIFEST.md` | `DEV_JOURNAL.md` |
|---|---|---|
| Planning a new sub-phase | Add `[ ]` task items with full spec | Nothing yet |
| Completing a sub-phase | Flip to `[x]`. Nothing else. | Append one strict entry |
| Architectural decision | Never in the manifest body | Never in the journal — goes in blueprint or `SCHEMA_EVOLUTION.MD` |
| Audit finding (F1–Fn) | Never | Never — belongs in code comments |
| Deferred item / tech debt | Entry in `docs/TECH_DEBT_BACKLOG.md` | One-line `Deferred: DEBT-N` reference |

### Git Commit Protocol (Windows / PowerShell, strict)

Git execution is **strictly non-autonomous**: do NOT run Git commands yourself. At the absolute end of your response, provide the precise block of commands for the user to run, following these zero-friction rules:

1. **No multiline strings:** You are prohibited from using Bash heredocs (`<<EOF`) or PowerShell here-strings (`@'` … `'@`).
2. **Inline double-flag format:** Use `git commit -m "feat: title here" -m "Brief 1-2 sentence description."` Commit messages follow Conventional Commits (`feat(core): …`, `test(memory): …`, `docs(charter): …`).
3. **No novels in commits:** The commit body must be a concise summary. Verbose architectural detail belongs only in `docs/DEV_JOURNAL.md` or PR notes — never in the commit message.
4. **Granular adds:** Write each `git add` on its own line (one file per line), never inline-chained or backslash-continued, which breaks in some PowerShell environments.
5. **Main-only workflow — no branches:** This is a solo-developer project. NEVER suggest or generate `git checkout -b`, `git branch`, `git switch -c`, or `git push origin <branch>`. All work commits directly to `main`; the only valid push is `git push origin main`. For isolation, use `git stash` and note the stash ref.
