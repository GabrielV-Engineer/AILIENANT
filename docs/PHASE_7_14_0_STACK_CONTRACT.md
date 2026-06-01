# Phase 7.14.0 — Ratified Stack, Theming & Bundle Contract

> **Binding contract** for the Phase 7.14 frontend track (Zero-Bubble canvas + Elite Diff Engine).
> This document is the *machine-checkable* companion to the narrative ADRs in
> [`PHASE_7_14_BLUEPRINT.md`](PHASE_7_14_BLUEPRINT.md) (ADR-720 → ADR-726). Where the blueprint
> states intent, this file pins exact versions, licenses, the bundle ceiling, and the two
> engineering blind-spots that must be resolved *before* 7.14.2 writes any rendering code.
>
> 7.14.0 produces **no runtime change** — no `package.json` edit, no `.ts/.tsx/.css/.py` change.
> The dependencies below are added when **7.14.2 (Elite Diff Engine)** lands.

---

## 1. Pinned dependency table (to add to `ailienant-extension/package.json` at 7.14.2)

| Package | Version (resolved) | License | Role |
|---|---|---|---|
| `diff` (jsdiff) | `7.0.0` (pinned `^7.0.0`) | BSD-3-Clause | Line/word diff math. Already present transitively (via `@vscode/test-cli` → `mocha` → `diff@7.0.0`, deduped); promoted to an explicit `dependencies` entry so a dev-dep prune cannot break the webview. |
| `react-diff-viewer-continued` | `4.2.2` | MIT | Asymmetric split-grid diff view. (Vendors its own `diff@9.0.0` internally — separate from our explicit `diff@7.0.0`.) |
| ~~`shiki`~~ | *(not shipped at 7.14.2)* | MIT | **Deferred — DEBT-003.** Syntax tokenization of diffs. Measured inline weight is prohibitive (JS engine ~160 KB + smallest usable grammar ~172 KB), and externalizing assets was rejected as too much CSP/runtime-loader risk. 7.14.2 renders diffs as themed monospace; tokenization is deferred to a future phase (requires runtime asset externalization or a worker). See `docs/TECH_DEBT_BACKLOG.md` (DEBT-003). |

> **Resolved at 7.14.2 install (2026-06-01):** `npm ls` → `diff@7.0.0`, `react-diff-viewer-continued@4.2.2`. Licenses inspected: all permissive (BSD-3-Clause / MIT). No copyleft in the dependency closure. **shiki was trialed, measured, and removed** — see DEBT-003.

**License-check rule (binding, executed at 7.14.2 install):**

1. `npm ls --all diff react-diff-viewer-continued shiki` to capture the exact resolved versions.
2. Inspect `node_modules/<pkg>/LICENSE` (and transitive deps) — confirm every license is
   permissive (MIT / BSD-* / ISC / Apache-2.0). No copyleft (GPL/LGPL/AGPL) is admissible in the
   shipped webview bundle.
3. Write the exact resolved versions + confirmed licenses back into this table at that time.

---

## 2. Bundle budget (binding ceiling)

| Metric | Value |
|---|---|
| **Baseline** — production-minified `dist/workspace.js` | **354,338 B (~346 KB)** — measured 2026-06-01 |
| Reference — un-minified dev `dist/workspace.js` | 1,587,307 B |
| ~~Original ceiling~~ | ~~500 KB~~ — superseded, see amendment below |
| **Ceiling (binding, amended at 7.14.2)** — production-minified `dist/workspace.js` | **550 KB (563,200 B)** |
| Measured at 7.14.2 close | **549,335 B (~536 KB)** — under ceiling |

**Ceiling amendment (2026-06-01, 7.14.2).** The original 500 KB ceiling assumed `shiki` could be
fine-grained-loaded inline within ~150 KB of headroom. Empirical measurement disproved this: the JS
regex engine alone is ~160 KB minified and the smallest usable grammar (`tsx`, which embeds JS/TS) is
~172 KB — over 330 KB before any theme, far exceeding the headroom. `react-diff-viewer-continued`
itself (~183 KB) already pushes the base to ~537 KB. Externalizing assets (Mechanism A) and
hand-rolling a diff renderer were both rejected (CSP/runtime-loader risk; reinvention risk). The
ratified pivot: **ship `jsdiff` + `react-diff-viewer-continued` only, defer syntax tokenization
(DEBT-003), and amend the ceiling to 550 KB** — a deliberate, bounded raise that fits the split-grid
diff while explicitly omitting the syntax highlighter to protect Time-to-Interactive. The **7.14.2 DoD
fails the gate above 550 KB (563,200 B).** This remains a hard cap, not an open-ended allowance.

**Measurement command (reproducible):**

```powershell
cd C:\Proyectos\Proyect_Ailienant\ailienant-extension
node esbuild.js --production
(Get-Item dist/workspace.js).Length
```

`dist/` is gitignored, so this rebuild never dirties the tree.

---

## 3. shiki lazy-load + fine-grained-core rule (ADR-722) — IIFE caveat made binding

> **Superseded at 7.14.2 (DEBT-003).** shiki is **not shipped** in 7.14.2 — measured inline weight is
> incompatible with the bundle ceiling and the externalize alternative was rejected (see §2 amendment
> and `docs/TECH_DEBT_BACKLOG.md`). The rules below stand as the binding contract for *whenever shiki
> is reintroduced* (the no-WASM JS engine + fine-grained core + lazy-load constraints remain correct);
> they are dormant, not deleted.

- Use **`shiki/core`** + `createHighlighterCore` with **explicit** language and theme imports.
  Never import the all-grammar default `shiki` export (it bundles every grammar and blows §2).
- Restrict the loaded grammars to languages actually rendered in chat/diffs — proposed allow-list:
  `ts`, `tsx`, `js`, `jsx`, `py`, `json`, `bash`/`shell`, `css`, `html`, `md`. Finalize at 7.14.2.

### CRITICAL — esbuild `iife` does NOT code-split

`dist/workspace.js` is built with `format: 'iife'`
([`esbuild.js:62`](../ailienant-extension/esbuild.js#L62)). **esbuild does not support
`splitting` for the `iife` format.** Therefore a bare `await import('shiki')` in the workspace
bundle will **not** yield a lazily-fetched chunk — esbuild either statically inlines shiki into
`workspace.js` (no lazy win, breaches the §2 ceiling) or errors. Relying on a bare dynamic import
to "lazy-load shiki" in this bundle is **forbidden**.

7.14.2 MUST pick **one** of the following lazy mechanisms explicitly:

- **(A) Externalize + URI-load (preferred).** Add `external: ['shiki', 'shiki/core', …]` to the
  workspace esbuild context ([`esbuild.js:62`](../ailienant-extension/esbuild.js#L62)) and load
  shiki's WASM + grammar/theme assets at runtime via `webview.asWebviewUri(...)` from `media/` or
  `dist/`. Keeps the IIFE format; shiki never enters the critical bundle.
- **(B) Switch the workspace bundle to `format: 'esm'` + `splitting: true`** (mirroring the
  dashboard context at [`esbuild.js:77`](../ailienant-extension/esbuild.js#L77)) — **only if** the
  VS Code webview HTML for this panel can load an ESM module script (`<script type="module">` with
  a CSP `script-src` that permits it). This MUST be validated before committing to it; the panel's
  current HTML loads a classic IIFE script.

This contract does not pick A vs B (that is 7.14.2 implementation work). It **binds** 7.14.2 to
resolve the no-code-split fact head-on and forbids assuming `await import()` alone lazy-loads in IIFE.

---

## 4. No-per-token-rehighlight streaming rule (ADR-722)

- **Highlight on block-complete only.** A fenced code block is tokenized by shiki once, when its
  closing fence arrives — never on each streamed token.
- **Diff on edit-arrival only.** A diff is computed once, when its `ApplyWorkspaceEditPayload`
  arrives host-side — never re-diffed per chunk.
- Rationale: per-token re-tokenization / re-diffing pegs the webview main thread (CPU + jank) on
  long generations.

---

## 5. Large-diff DOM guard (binding) — `react-diff-viewer-continued` reconciliation cost

`react-diff-viewer-continued` renders **O(N) DOM rows** for an N-line diff and reconciles them
synchronously. A multi-thousand-line workspace edit would mount thousands of nodes at once and
freeze the webview main thread (kills Time-to-Interactive). The blueprint's "collapse oversized
hunks" guard (DoD row **DF4**: "2k-line diff stays responsive") is hereby hardened into a concrete,
non-optional directive:

- **Hard line cap (binding):** a `DIFF_RENDER_LINE_CAP` constant — proposed **400 changed lines**
  per diff block (finalize at 7.14.2). Beyond the cap, do **not** render all rows.
- 7.14.2 MUST implement **one** of:
  - **Collapse-by-default (preferred, cheapest, matches the blueprint's "show more"):** render only
    the changed hunks plus a small context window; collapse the remainder behind a "show N more
    lines" affordance.
  - **Row virtualization:** only mount the rows in/near the viewport.
- **Unbounded full-file rendering is forbidden.** This directive is the mechanism behind the
  existing DF4 DoD row, named here so 7.14.2 cannot ship a diff engine without it.

---

## 6. Theming contract (ADR-722)

The diff and code surfaces bind **only** to VS Code theme variables — no hard-coded hex
reds/greens — so a theme switch repaints without reload. The seam already exists: `.ws-md-pre` /
`.ws-mini-terminal` key off `--vscode-editor-background` / `--vscode-editor-font-family` in
[`workspace.css`](../ailienant-extension/src/workspace/workspace.css). The diff extends it with:

| Surface | CSS variable |
|---|---|
| Inserted-line background | `var(--vscode-diffEditor-insertedTextBackground)` |
| Removed-line background | `var(--vscode-diffEditor-removedTextBackground)` |
| Code/diff background | `var(--vscode-editor-background)` |
| Monospace font | `var(--vscode-editor-font-family)` |

shiki's theme follows the active VS Code theme (load both a light and dark shiki theme, switch by
the webview's `body` theme class / `--vscode-*` cue).

---

## 7. No Python contract drift (ADR-721)

The inline diff source is **host-side enrichment** of the existing `server_apply_workspace_edit`
seam (the host already reads the old document text in `PatchActuator`). The host posts a new
**webview** message `RENDER_DIFF {patch_id, file_path, old_content, new_content, status}`. There is
**no** change to `ailienant-core/api/ws_contracts.py`, `AIlienantGraphState`, or any payload shape,
and **no** new server event. The 7.14 frontend track stays orthogonal to the backend 8.0.0 track.
