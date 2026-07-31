#!/usr/bin/env node
// Boots a hermetically-seeded ailienant-core backend for the Playwright dashboard
// suite (Phase 11.9). Storage is pointed at a scratch temp directory via
// AILIENANT_CATALOG_DB / AILIENANT_GRAPHRAG_LANCEDB — the only two overrides that
// keep the seed and the server off the developer's real ~/.ailienant home — then
// the fixture seed script runs to completion before uvicorn starts, so the
// Playwright suite never races a cold dashboard against an empty store.
//
// Port 8731 is a fixed literal, duplicated (not imported) in playwright.config.ts:
// the two files run in different module systems (this one is real ESM via the
// .mjs extension; the Playwright config is transformed to CJS), so a shared
// import would trip over the ESM/CJS boundary for one extra constant. Keep them
// in sync if either changes.
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
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
const env = {
    ...process.env,
    AILIENANT_CATALOG_DB: join(scratch, 'catalog.sqlite'),
    AILIENANT_GRAPHRAG_LANCEDB: join(scratch, 'lancedb'),
    AILIENANT_API_PORT: PORT,
};
// Dev-mode auth bypass: main.py's auth middleware no-ops when this is absent, so
// the Playwright suite needs no token plumbing to reach same-origin endpoints.
delete env.AILIENANT_AUTH_TOKEN;

const seed = spawnSync(python, ['tests/e2e/seed_dashboard_fixture.py'], {
    cwd: backendPath,
    env,
    encoding: 'utf8',
});
if (seed.status !== 0) {
    console.error('[e2e] Fixture seed failed:\n' + seed.stdout + '\n' + seed.stderr);
    process.exit(seed.status ?? 1);
}
const lastLine = seed.stdout.trim().split('\n').filter(Boolean).pop() ?? '{}';
const ids = JSON.parse(lastLine);

const fixtureDir = resolve(__dirname, '.fixture-data');
mkdirSync(fixtureDir, { recursive: true });
writeFileSync(join(fixtureDir, 'ids.json'), JSON.stringify(ids, null, 2));
console.log('[e2e] Seeded fixture projects:', ids);

const server = spawn(
    python,
    ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', PORT],
    { cwd: backendPath, env, stdio: 'inherit' },
);

for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => { server.kill(sig); process.exit(0); });
}
server.on('exit', (code) => process.exit(code ?? 0));
