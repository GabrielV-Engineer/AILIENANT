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
        timeout: 60_000,
        reuseExistingServer: false,
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    ],
});
