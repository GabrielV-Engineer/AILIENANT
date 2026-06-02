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
### DEBT-NNN — Short description
- **Date:** YYYY-MM-DD
- **Reproduce:** exact shell command that surfaces the error
- **File(s):** affected path(s) and line numbers if known
- **Error:** mypy error code or free-text description
- **Blocked by:** external dependency / phase prerequisite (if any)
- **Phase:** which Phase 8 subfase will address this
- **Notes:** context for future reader
```

---

## Open Entries

### DEBT-001 — tools.patch_tool: LangChain @tool decorator stub mismatch

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict tools/patch_tool.py`
- **File:** `tools/patch_tool.py:219`
- **Error:** `unused-ignore[misc]` — `# type: ignore[misc]` was added when LangChain's `@tool`
  decorator lacked stubs; the stubs may now be present, making the comment redundant under
  `--strict` (which flags stale ignores).
- **Blocked by:** LangChain / langchain-core stub completeness. Verify after
  `pip install --upgrade langchain-core`.
- **Phase:** 8.1.C
- **Notes:** Currently suppressed via `[mypy-tools.patch_tool] follow_imports = silent` in
  `mypy.ini`. If stubs are still absent, the entry must remain and the silencing block stays.
  If stubs landed, remove the `# type: ignore[misc]` comment and the silencing block together.

---

### DEBT-002 — agents/contract_guard.py: MODEL_MEDIUM not explicitly exported

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict agents/contract_guard.py`
- **File:** `agents/contract_guard.py:100`
- **Error:** `attr-defined` — `Module "tools.llm_gateway" does not explicitly export attribute
  "MODEL_MEDIUM"`
- **Blocked by:** `tools.llm_gateway` silenced (`follow_imports = silent`). The error is invisible
  under the current `mypy .` gate but surfaces under `--strict`.
- **Phase:** 8.2 (resolves automatically when llm_gateway is unsilenced, IF `MODEL_MEDIUM` is
  added to `__all__` or the import is moved to `shared/config.py`).
- **Notes:** `MODEL_MEDIUM` is defined in `tools/llm_gateway.py` but is not in `__all__`. Two
  remediation options: (a) add `MODEL_MEDIUM` (and `MODEL_SMALL`, `MODEL_BIG`) to `__all__` in
  llm_gateway.py; (b) move the constants to `shared/config.py` (where they may semantically
  belong) and update all callers. Option (b) is cleaner long-term.

---

### DEBT-003 — brain/swarms.py: BaseCheckpointSaver missing type args

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:189`
- **Error:** `type-arg` — `Missing type arguments for generic type "BaseCheckpointSaver"`
- **Blocked by:** None (fixable now, but out of scope for Phase 8.0)
- **Phase:** 8.4
- **Notes:** `BaseCheckpointSaver` is a LangGraph generic. The fix is likely
  `BaseCheckpointSaver[Any]` (import `Any` from `typing`). Verify by checking the LangGraph type
  stub for `BaseCheckpointSaver.__class_getitem__` support.

---

### DEBT-004 — brain/swarms.py: stale unused-ignore comment

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:226`
- **Error:** `unused-ignore` — `# type: ignore` comment no longer needed (stubs improved or code
  changed to not trigger the original error)
- **Blocked by:** None
- **Phase:** 8.4 (fix together with DEBT-003 in the same atomic commit for swarms.py)
- **Notes:** Always remove stale ignores together with the associated type-arg fix to keep the
  diff atomic and the intent clear.

---

### DEBT-005 — Multiple brain/ + agents/ interior nodes: unknown strict debt

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | head -60`
- **Files:** `brain/engine.py`, `brain/ideation.py`, `brain/guardrails.py`,
  `brain/intent_router.py`, `agents/coder.py`
- **Error:** Various — `type-arg`, `no-any-return`, `no-untyped-def` (full count hidden behind
  silenced transitive deps; cannot be measured accurately until Phases 8.1–8.5 remove the wall)
- **Blocked by:** Silenced modules (`tools.llm_gateway`, `agents.analyst`, `core.db`, etc.)
  masking the true error count.
- **Phase:** 8.4 and 8.7 (assess after each unsilencing step)
- **Notes:** Do NOT attempt to fix these preemptively. Run an exploratory `mypy --strict` after
  each Phase 8.1–8.5 step to update this entry's error count. The topological order guarantees
  these errors only become actionable after their upstream deps are clean.

---

### DEBT-006 — Inline diff has no syntax highlighting (shiki deferred)

> **Note on numbering:** this is the item the 7.14.2 directive referred to as "DEBT-003"; that id was
> already taken by the `brain/swarms.py` entry above, so it is filed here as DEBT-006. Treat
> "DEBT-003 (shiki)" as an alias for this entry.

- **Date:** 2026-06-01
- **Reproduce:** `cd ailienant-extension && node esbuild.js --production; (Get-Item dist/workspace.js).Length`
  (with shiki re-added inline, the bundle exceeds the 550 KB ceiling).
- **File(s):** `src/workspace/components/DiffBlock.tsx` (diff cells render as themed monospace, no
  token spans).
- **Error:** Feature gap, not a type error. The Elite Diff Engine ships without VS Code-identical
  syntax tokenization. Measured: shiki's JS regex engine (~160 KB minified) + the smallest usable
  grammar `tsx` (~172 KB) exceed the bundle headroom; `react-diff-viewer-continued` alone already
  brings the base to ~537 KB.
- **Blocked by:** The IIFE webview bundle cannot lazily code-split (esbuild `iife` has no
  `splitting`). Reintroducing shiki requires runtime asset externalization (Mechanism A:
  `external:[shiki]` + ship assets to `media/` + `webview.asWebviewUri` loader) or a worker — both
  deferred as out-of-scope CSP/plumbing risk for 7.14.2.
- **Phase:** A future 7.14.x or Phase 11 polish slice.
- **Notes:** The dormant shiki contract (no-WASM JS engine, fine-grained core, lazy-load) is preserved
  in `docs/PHASE_7_14_0_STACK_CONTRACT.md` §3 for whenever this is picked up. ADR-722's *theming*
  half is already honored — diff colors bind to `--vscode-diffEditor-*` CSS vars today; only the
  token layer is missing.
- **Audit cross-ref:** the Phase 7.15 pre-checkpoint audit's "code blocks render as plain white text"
  complaint maps to THIS entry — it is the known, deliberate shiki deferral, not a new defect. The 7.15
  remediation does **not** re-open it (see Fase 7.15 in `PROJECT_MANIFEST.md`); highlighting stays here
  until the bundle/externalization plumbing is funded.

### DEBT-007 — Auto-accept low-risk edits pays a full HITL round-trip (shift-left candidate)

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
  today; the setting is webview-local). Adding it is out of scope for the UI slice that introduced the
  toggle.
- **Phase:** A future shift-left optimization (Phase 11 or a later 7.14.x): the backend reads the
  auto-accept setting and, for low-risk edits, **omits emitting the approval event altogether** — the
  edit applies server-side with no round-trip. Trade-off accepted now for implementation simplicity
  (reuse `useHitlResponder`'s wire message, zero new Python logic).
- **Notes:** The conservative risk gate (any medium/high metric forces the manual card) must be
  preserved if/when this moves server-side.

---

## Closed Entries

*(Move entries here when their Phase has been executed and verified.)*

---

## Appendix: Reproduction Quick-Reference

```powershell
# Run from: C:\Proyectos\Proyect_Ailienant\ailienant-core

# DEBT-001
.\venv\Scripts\python -m mypy --strict tools/patch_tool.py

# DEBT-002
.\venv\Scripts\python -m mypy --strict agents/contract_guard.py

# DEBT-003 + DEBT-004
.\venv\Scripts\python -m mypy --strict brain/swarms.py

# DEBT-005 (exploratory — count changes over time)
.\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l
```
