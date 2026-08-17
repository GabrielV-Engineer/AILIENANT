## Context

`ailienant-core` is a Python/FastAPI backend with LanceDB and tree-sitter native dependencies; `ailienant-extension` is a VS Code extension that today expects a developer-provisioned venv. There is no containerized or binary distribution path yet. See `proposal.md` - Why for the launch-readiness motivation.

## Goals / Non-Goals

**Goals:**
- Two independent install paths for two audiences: `docker compose up` for anyone comfortable with Docker, and a bundled binary for a VS Code user with none of the toolchain installed.
- Keep both paths exercised by the same closing checkpoint gate (13.5), so neither can silently regress relative to the other.

**Non-Goals:**
- Auto-update / release-channel infrastructure for the binary — out of scope for this phase; first-install only.
- Multi-arch container images beyond what's needed to run the stack locally (no registry publishing pipeline here).

## Decisions

- **Container stack lives at repo root** (`Dockerfile`, `docker-compose.yml`), not nested under `ailienant-core/`, so `docker compose up` from the repo root is the documented single command — matches how `DEVELOPERS.md` already documents repo-root as the orientation point for a new contributor.
- **Binary compiler choice (PyInstaller vs. Nuitka) is deferred** — see Open Questions. Both satisfy "single per-OS executable bundling FastAPI + LanceDB + tree-sitter"; the choice affects build tooling, not the spec-level behavior in `specs/portfolio-release/spec.md`, so it doesn't block task breakdown.
- **The extension bootstraps the binary, it doesn't bundle it inside the `.vsix`.** Bundling a per-OS binary inside the extension package would multiply `.vsix` size by platform count; downloading/unpacking it on first activation (per CLAUDE.md §5.6 cross-platform safety — `pathlib`, atomic `os.replace`, closed handles before replace) keeps the package small and matches how other VS Code extensions with native binaries (e.g. language servers) handle this.

## Risks / Trade-offs

- [Binary build breaks on a platform tree-sitter/LanceDB don't ship a matching wheel for] → mitigate by pinning supported OS/arch combinations explicitly in `13.2`'s DoD rather than promising universal support; the "unsupported platform" scenario in the spec exists precisely to make this an explicit, tested failure mode rather than an implicit gap.
- [Docker and binary paths drift out of sync over time (one gets tested more than the other)] → the Final Checkpoint Gate (`13.5`) requirement exercises both paths together specifically to prevent this.

## Open Questions

- PyInstaller vs. Nuitka for `ailienant-core` — resolve at `13.2` implementation time based on a quick empirical spike (binary size, cold-start time, and tree-sitter/LanceDB native-extension compatibility); doesn't change the spec or task breakdown either way.
- Exact diagram toolchain for `13.3` (hand-drawn vs. a generated-from-code diagram tool) — deferred to when that task starts; doesn't affect any other task.
