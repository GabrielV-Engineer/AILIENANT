/**
 * Phase 11.9 — Dashboard Checkpoint Gate (Playwright / dashboard SPA half).
 *
 * Covers the four invariants that live in the browser-reachable dashboard SPA
 * (`src/dashboard/`), against a hermetically-seeded backend (see
 * `run-backend.mjs` / `../../ailienant-core/tests/e2e/seed_dashboard_fixture.py`).
 * The remaining four 11.9 invariants (ActiveTaskHeader, ReasoningStream,
 * SessionSummaryCard, auto-accept) live in the VS Code webview and are covered
 * separately by `src/test/phase11_9_dashboard_checkpoint_gate.test.ts`.
 */
import { test, expect } from '@playwright/test';
import { readFixtureIds } from './fixtures';

const fixture = readFixtureIds();

test.describe('11.9 — Dashboard SPA', () => {
    test('all panels load', async ({ page }) => {
        await page.goto('/dashboard');
        const navItems = page.locator('.db-nav-item');
        const count = await navItems.count();
        expect(count).toBeGreaterThan(0);

        for (let i = 0; i < count; i++) {
            const item = navItems.nth(i);
            await item.click();
            await expect(item).toHaveAttribute('data-active', 'true');
            // The panel root must render something — no blank main area.
            await expect(page.locator('main.db-main')).not.toBeEmpty();
        }
    });

    test('project context selector re-scopes data on switch', async ({ page }) => {
        await page.goto('/dashboard');
        const hitlTile = page.locator('.db-card', { hasText: 'HITL pending' });
        const select = page.getByLabel('Active project');

        await select.selectOption(fixture.proj_a);
        await expect(hitlTile.locator('.ui-stat-sub')).toContainText('3 total events');
        await expect(hitlTile.locator('.ui-stat-value')).toHaveText('1');

        await select.selectOption(fixture.proj_b);
        await expect(hitlTile.locator('.ui-stat-sub')).toContainText('0 total events');
        await expect(hitlTile.locator('.ui-stat-value')).toHaveText('0');
    });

    test('GraphRAG graph renders with >=1 node and a god-node badge', async ({ page }) => {
        await page.goto('/dashboard');
        await page.locator('.db-nav-item', { hasText: 'Memory Management' }).click();

        // Each `.mm-section-item` button carries `title={abs_prefix}` (SectionsList.tsx),
        // which starts with the seeded project's root — a stable way to pick the
        // proj-a section without depending on its folder label.
        const projectASection = page.locator(`.mm-section-item[title*="${fixture.proj_a_root}"]`);
        await projectASection.click();

        // Force the DOM-based 2D graph (the 3D nebula is a canvas — not
        // reliably clickable across headless-Chromium WebGL configurations).
        await page.locator('.ai-btn', { hasText: 'Graph 2D' }).click();

        await expect(page.locator('.mm-graph .react-flow__node')).not.toHaveCount(0);

        // Search highlights the hub node regardless of the LOD level ReactFlow
        // picked (full/medium/dot), sidestepping zoom-dependent node markup.
        await page.locator('.mm-search input').fill('hub');
        const hubNode = page.locator('.mm-node-hit').first();
        await expect(hubNode).toBeVisible();
        await hubNode.click();

        await expect(page.locator('.mm-detail-badges')).toContainText('hub');
    });

    test('vector map heatmap is visible', async ({ page }) => {
        await page.goto('/dashboard');
        await page.locator('.db-nav-item', { hasText: 'Memory Management' }).click();

        const projectASection = page.locator(`.mm-section-item[title*="${fixture.proj_a_root}"]`);
        await projectASection.click();

        await page.locator('.ai-btn', { hasText: 'Vector map' }).click();

        const canvas = page.locator('.mm-scatter-canvas');
        await expect(canvas).toBeVisible();
        const box = await canvas.boundingBox();
        expect(box?.width).toBeGreaterThan(0);
        expect(box?.height).toBeGreaterThan(0);

        await expect(page.locator('.mm-vec-caption')).toContainText('PCA projection');
    });
});
