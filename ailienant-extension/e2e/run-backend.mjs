#!/usr/bin/env node
// Boots a hermetically-seeded ailienant-core backend for the Playwright dashboard
// suite (Phase 11.9). Storage is pointed at a scratch temp directory via
// AILIENANT_CATALOG_DB / AILIENANT_GRAPHRAG_LANCEDB — the only two overrides that
// keep the seed and the server off the developer's real ~/.ailienant home.
//
// The fixture seed itself runs IN-PROCESS inside uvicorn's own startup
// (main.py's lifespan, gated on AILIENANT_E2E_SEED_IDS_PATH below), not as a
// separate `spawnSync` Python process ahead of it — that used to cold-import
// the shared heavy dependency tree (lancedb, litellm-adjacent config,
// tree-sitter parsers) twice, serially, inside Playwright's single webServer
// wait budget. Seed-before-serve is still guaranteed: the hook runs before
// `yield` in the lifespan, so the app can't answer a request — including
// Playwright's own readiness poll — until seeding has finished and
// AILIENANT_E2E_SEED_IDS_PATH has been written. This script therefore only
// needs to ensure that path's parent directory exists before uvicorn starts;
// it never reads the file back itself (the Playwright spec reads it directly
// off disk once the server is ready).
//
// Port 8731 is a fixed literal, duplicated (not imported) in playwright.config.ts:
// the two files run in different module systems (this one is real ESM via the
// .mjs extension; the Playwright config is transformed to CJS), so a shared
// import would trip over the ESM/CJS boundary for one extra constant. Keep them
// in sync if either changes.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = process.env.AILIENANT_API_PORT || '8731';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const backendPath = resolve(__dirname, '..', '..', 'ailienant-core');

const PYTHON_CANDIDATES = [
    join(backendPath, '.venv', 'Scripts', 'python.exe'),
    join(backendPath, 'venv', 'Scripts', 'python.exe'),
    join(backendPath, '.venv', 'bin', 'python'),
    join(backendPath, 'venv', 'bin', 'python'),
];
const python = PYTHON_CANDIDATES.find(existsSync);
if (!python) {
    console.error(
        '[e2e] No Python virtualenv found under ailienant-core/.venv or ailienant-core/venv. ' +
        'Create one and install ailienant-core/requirements.txt before running the Playwright suite.',
    );
    process.exit(1);
}

const scratch = mkdtempSync(join(tmpdir(), 'ailienant-e2e-'));
// main.py's lifespan writes the seeded ids here once _seed() completes, before
// the app can serve any request — mkdir it now so that write never races a
// missing parent directory.
const fixtureDir = resolve(__dirname, '.fixture-data');
mkdirSync(fixtureDir, { recursive: true });

const env = {
    ...process.env,
    AILIENANT_CATALOG_DB: join(scratch, 'catalog.sqlite'),
    AILIENANT_GRAPHRAG_LANCEDB: join(scratch, 'lancedb'),
    AILIENANT_API_PORT: PORT,
    AILIENANT_E2E_SEED_IDS_PATH: join(fixtureDir, 'ids.json'),
};
// Dev-mode auth bypass: main.py's auth middleware no-ops when this is absent, so
// the Playwright suite needs no token plumbing to reach same-origin endpoints.
delete env.AILIENANT_AUTH_TOKEN;

console.log(`[e2e] spawning uvicorn at ${new Date().toISOString()}`);
const server = spawn(
    python,
    ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', PORT],
    { cwd: backendPath, env, stdio: 'inherit' },
);

for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => { server.kill(sig); process.exit(0); });
}
server.on('exit', (code) => process.exit(code ?? 0));
