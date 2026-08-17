## Why

Phase 13 is the final pre-launch phase: AILIENANT must go from "runs on the developer's machine with a manually provisioned venv" to "a stranger can install and run it in one command." No `docs/PHASE_13_BLUEPRINT.md` exists yet, and this is the first phase piloting the OpenSpec verification layer (see `docs/TECH_DEBT_BACKLOG.md` DEBT-165) — the WBS items already committed in `docs/PROJECT_MANIFEST.md` (`13.1`-`13.5`) are captured here as OpenSpec requirements/scenarios instead of a traditional blueprint, so `openspec validate` can check them are well-formed as an ADVISORY CI gate.

## What Changes

- Full Dockerization: `Dockerfile` + `docker-compose.yml` launching the full architecture (backend + LanceDB) with a single command.
- Binary packaging for zero-friction install: PyInstaller/Nuitka-compiled per-OS binary for `ailienant-core`; the VS Code extension unpacks and runs it in the background with no user-installed Python/Docker/Node.
- Visual documentation: `README.md` finalized with real architecture diagrams.
- Autonomous demo: a recording of TestAgent + LogicAgent + AnalystAgent solving a cyclic bug unattended.
- Final checkpoint gate: a zero-friction-install end-to-end validation closing the phase.

## Capabilities

### New Capabilities
- `portfolio-release`: the release-readiness requirements for AILIENANT's public launch — containerized deployment, zero-dependency binary install, finalized visual docs, an autonomous multi-agent demo, and the closing E2E validation gate.

### Modified Capabilities
(none — this is a new capability area; nothing under `openspec/specs/` exists yet to modify)

## Impact

- New: `Dockerfile`, `docker-compose.yml` (repo root or `ailienant-core/`, TBD at design time).
- New: PyInstaller/Nuitka build config for `ailienant-core`; extension-side binary bootstrap logic in `ailienant-extension/src/`.
- Modified: `README.md` (+ translations) for final architecture diagrams.
- No changes to `docs/PROJECT_MANIFEST.md`'s `13.1`-`13.5` checkbox items themselves — this proposal supplies the binding requirement/scenario detail; the manifest keeps its role as the master WBS index.
