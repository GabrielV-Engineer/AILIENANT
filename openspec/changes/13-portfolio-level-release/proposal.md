## Why

Phase 13 is the final pre-launch phase: AILIENANT must go from "runs on the developer's machine with a manually provisioned venv" to "a stranger can install and run it in one command." No `docs/PHASE_13_BLUEPRINT.md` exists yet, and this is the first phase piloting the OpenSpec verification layer (see `docs/TECH_DEBT_BACKLOG.md` DEBT-165) — the WBS items already committed in `docs/PROJECT_MANIFEST.md` (`13.1`-`13.5`) are captured here as OpenSpec requirements/scenarios instead of a traditional blueprint, so `openspec validate` can check them are well-formed as an ADVISORY CI gate.

A gap surfaced during a Phase 13 status review: `13.1`-`13.5` cover the *end-user* install experience only. There was no scope covering how a change safely gets from the maintainer's machine into a published release — branch protection was documented but never switched on, and no versioned release pipeline exists for the extension. `13.6` (below) closes that gap. It is deliberately scoped as internal/contributor-facing tooling, kept separate from `13.1`-`13.5` so the two audiences — the end user installing AILIENANT to help them code, versus the maintainer(s) shipping it — are never conflated in either the spec or the implementation.

## What Changes

- Full Dockerization: `Dockerfile` + `docker-compose.yml` launching the full architecture (backend + LanceDB) with a single command.
- Binary packaging for zero-friction install: PyInstaller/Nuitka-compiled per-OS binary for `ailienant-core`; the VS Code extension unpacks and runs it in the background with no user-installed Python/Docker/Node.
- Visual documentation: `README.md` finalized with real architecture diagrams.
- Autonomous demo: a recording of TestAgent + LogicAgent + AnalystAgent solving a cyclic bug unattended.
- Final checkpoint gate: a zero-friction-install end-to-end validation closing the phase.
- **Contributor & release pipeline (internal/maintainer-facing, not part of the end-user install experience above):** enable the `main` branch-protection ruleset requiring the `backend-gate` and `frontend-gate` status checks; establish a versioned release pipeline for the VS Code extension (semver policy, `publisher` field, `vsce` packaging, tag-triggered `vsce publish` + GitHub Release).

## Capabilities

### New Capabilities
- `portfolio-release`: the release-readiness requirements for AILIENANT's public launch — containerized deployment, zero-dependency binary install, finalized visual docs, an autonomous multi-agent demo, the closing E2E validation gate, and the maintainer-side branch-protection/release pipeline that ships each of those artifacts safely.

### Modified Capabilities
(none — this is a new capability area; nothing under `openspec/specs/` exists yet to modify)

## Impact

- New: `Dockerfile`, `docker-compose.yml` (repo root or `ailienant-core/`, TBD at design time).
- New: PyInstaller/Nuitka build config for `ailienant-core`; extension-side binary bootstrap logic in `ailienant-extension/src/`.
- Modified: `README.md` (+ translations) for final architecture diagrams.
- New (13.6, internal-only): `.github/workflows/release.yml`; `publisher` field + `vsce` devDependency in `ailienant-extension/package.json`; a maintained `CHANGELOG.md`; a GitHub branch-protection ruleset on `main` (a repo setting, not a committable file).
- No changes to `docs/PROJECT_MANIFEST.md`'s `13.1`-`13.5` checkbox items themselves — this proposal supplies the binding requirement/scenario detail; the manifest keeps its role as the master WBS index. `13.6` was added to the manifest alongside this proposal update.
