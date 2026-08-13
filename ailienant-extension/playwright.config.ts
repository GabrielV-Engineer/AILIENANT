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
        // 60s -> 120s -> 180s all recurred on a fresh nightly venv (no bytecode
        // cache, shared/throttled CI CPU) because each raise was a guess, never
        // a measurement. `python -X importtime -c "import main"` proved the
        // real cost: litellm (~2.6s) and lancedb (~1s, mostly its unused
        // namespace REST client) were imported EAGERLY via 7 top-level call
        // sites across agents/planner.py, agents/researcher.py,
        // brain/summarizer.py, core/task_service.py, core/memory/
        // semantic_memory.py, core/janitor.py, and core/tool_rag.py's
        // `tool_rag_store` singleton — none needed until a real LLM/vector call
        // happens, never during this dashboard-only e2e suite. All deferred to
        // point-of-use (core/tool_rag.py's `tool_rag_store` also made
        // lazy-connect, since its constructor called `lancedb.connect()`
        // unconditionally at import time). 300s is real headroom on top of the
        // now-measured, much smaller cost — not a re-guess.
        timeout: 300_000,
        reuseExistingServer: false,
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    ],
});
