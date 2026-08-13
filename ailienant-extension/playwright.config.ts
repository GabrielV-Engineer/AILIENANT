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
        // 60s -> 120s -> 180s -> 300s all recurred on a fresh nightly venv.
        // 60->120->180 were blind guesses; 180->300 followed real, verified
        // work (`python -X importtime` proved litellm/lancedb were imported
        // eagerly via 7 top-level call sites, all deferred to point-of-use —
        // see docs/DEV_JOURNAL.md and DEBT-163/164) and STILL wasn't enough —
        // the full 300s elapsed with no uvicorn access-log line ever appearing
        // for a single /dashboard request. main.py's lifespan() now logs
        // elapsed time at each phase boundary (search "[startup]"/"[e2e]" in
        // the CI log) specifically so the NEXT failure, if any, points at a
        // real phase instead of another guess. 450s is a one-time hedge paired
        // with that instrumentation, not a re-guess — right-size down once a
        // real run's phase breakdown is known.
        timeout: 450_000,
        reuseExistingServer: false,
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    ],
});
