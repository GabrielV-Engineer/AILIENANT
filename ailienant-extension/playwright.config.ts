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
        // 60s wasn't enough on a cold CI runner's first-ever run (pip install +
        // uvicorn cold start) — bumped to 120s, which then recurred on every
        // nightly run (a fresh venv every time, not just a first-ever run).
        // Root cause fixed in run-backend.mjs/main.py (the fixture seed no
        // longer pays a second, fully separate cold Python/import cost ahead
        // of uvicorn); this margin is layered on top as insurance against
        // ordinary GH Actions runner speed variance, not a substitute for it.
        timeout: 180_000,
        reuseExistingServer: false,
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    ],
});
