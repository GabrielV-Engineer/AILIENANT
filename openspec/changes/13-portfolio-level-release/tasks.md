## 1. Full Dockerization (13.1)

- [ ] 1.1 Write `Dockerfile` for `ailienant-core` (FastAPI + LanceDB + tree-sitter native deps)
- [ ] 1.2 Write `docker-compose.yml` at repo root wiring the backend service and any persistent volume LanceDB needs
- [ ] 1.3 Verify `docker compose up` from a clean checkout reaches a healthy backend with no manual steps

## 2. Zero-Friction Binary Install (13.2)

- [ ] 2.1 Spike PyInstaller vs. Nuitka against `ailienant-core` (binary size, cold-start time, native-extension compatibility) — resolves design.md's open question
- [ ] 2.2 Build per-OS binaries (Windows/macOS/Linux) for `ailienant-core`
- [ ] 2.3 Implement extension-side bootstrap: download/unpack the matching binary on first activation, start it in the background
- [ ] 2.4 Handle the unsupported-platform case with a clear, actionable error (per the spec's "Unsupported platform" scenario)

## 3. Visual Documentation (13.3)

- [ ] 3.1 Produce architecture diagrams reflecting the actual shipped components and data flow
- [ ] 3.2 Update `README.md` with the finalized diagrams
- [ ] 3.3 Propagate the update to tracked translations (`README.es.md`, `README.fr.md`, `README.hi.md`, `README.it.md`, `README.ru.md`)

## 4. Autonomous Demo (13.4)

- [ ] 4.1 Seed a representative cyclic bug for the demo scenario
- [ ] 4.2 Record TestAgent + LogicAgent + AnalystAgent resolving it unattended, start to finish
- [ ] 4.3 Verify the recording captures the full unattended sequence with no human intervention

## 5. Final Checkpoint Gate (13.5)

- [ ] 5.1 Run the zero-friction binary install path end-to-end on a clean machine/VM
- [ ] 5.2 Run the single-command container launch path end-to-end
- [ ] 5.3 Record the gate result before Phase 13 is marked complete in `docs/PROJECT_MANIFEST.md`
