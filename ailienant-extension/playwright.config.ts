import { defineConfig, devices } from '@playwright/test';

// Kept a literal, duplicated (not imported) in e2e/run-backend.mjs — that file is
// real ESM (.mjs) while this config is transformed to CJS by Playwright, so a
// shared import would trip the ESM/CJS boundary for one constant. Keep in sync.
const PORT = 8731;

export default defineConfig({
    testDir: './e2e',
    timeout: 30_000,
    fullyParallel: false,
    retries: 0,
    reporter: [['list']],
    use: {
        baseURL: `http://127.0.0.1:${PORT}`,
        trace: 'retain-on-failure',
    },
    webServer: {
        command: 'node e2e/run-backend.mjs',
        url: `http://127.0.0.1:${PORT}/dashboard`,
        // History: 60s -> 120s -> 180s -> 300s all recurred (60->120->180 were
        // blind guesses; 180->300 followed real, measured import-latency work
        // — see docs/TECH_DEBT_BACKLOG.md DEBT-163/164 — and still wasn't
        // enough). Root cause turned out to be twofold: (1) e2e-nightly never
        // built dist/dashboard, so /dashboard was never actually mounted in
        // any nightly run; (2) most likely a transient GitHub Actions/platform
        // issue on the nights the 300s/450s runs failed — the same session
        // that saw those failures also hit a confirmed GitHub-side
        // `git push` "Internal Server Error". Once both were fixed, a real
        // `workflow_dispatch` run completed the FULL job (venv provision, npm
        // ci, esbuild, Playwright browser install, the suite itself) in 95s
        // total; a local dry-run with a fully fresh venv completed server
        // startup + all 4 tests in 16.2s. 120s (right-sized from the 450s
        // diagnostic hedge) gives ~7-8x margin over both measurements — tight
        // enough that a real future regression fails loud within two minutes
        // instead of silently eating 7.5 minutes of CI time every night.
        // main.py's lifespan() still logs elapsed time per phase (search
        // "[startup]"/"[e2e]" in the CI log) if this ever needs revisiting.
        timeout: 120_000,
        reuseExistingServer: false,
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    ],
});
